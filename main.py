import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from redis import Redis
from redis.exceptions import RedisError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

import crud
from auth import (
    authenticate_user,
    clear_login_cookie,
    create_email_verification_token,
    create_password_reset_token,
    csrf_token_for_request,
    find_user_by_email,
    get_current_user,
    get_optional_user,
    register_user,
    require_csrf,
    require_superuser,
    reset_password_with_token,
    set_csrf_cookie,
    set_login_cookie,
    verify_email_token,
)
from config import BASE_DIR, settings
from database import get_session, init_db
from downloader import extract_playlist_metadata
from mailer import send_email
from models import Playlist, PlaylistVideo, User, Video
from scheduler import scheduler, shutdown_scheduler, start_scheduler
from schemas import (
    BaseResponse,
    PlaylistAddRequest,
    PlaylistAddResponse,
    SyncIntervalRequest,
    SyncRequest,
    TaskResponse,
    TaskStatusResponse,
)
from storage import storage
from tasks import enqueue_sync, get_task_status, list_task_statuses

try:
    import sentry_sdk
except ImportError:  # pragma: no cover - optional production dependency
    sentry_sdk = None


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
ASSET_VERSION = "20260702-prefetch"

if settings.sentry_dsn and sentry_sdk:
    sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.environment)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await init_db()
    start_scheduler()
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "00:00"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_bytes(value: int | None) -> str:
    if not value:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


templates.env.filters["duration"] = format_duration
templates.env.filters["bytes"] = format_bytes


def safe_next_path(next_path: str | None) -> str:
    if next_path and next_path.startswith("/") and not next_path.startswith("//"):
        return next_path
    return "/"


def login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(f"/login?next={request.url.path}", status_code=status.HTTP_303_SEE_OTHER)


def render_template(request: Request, template_name: str, context: dict[str, object], status_code: int = 200):
    csrf_token = csrf_token_for_request(request)
    context = {
        **context,
        "csrf_token": csrf_token,
        "csrf_cookie_name": settings.csrf_cookie_name,
        "csrf_header_name": settings.csrf_header_name,
        "asset_version": ASSET_VERSION,
    }
    response = templates.TemplateResponse(request, template_name, context, status_code=status_code)
    set_csrf_cookie(response, csrf_token)
    return response


async def read_form_fields(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def storage_configured() -> bool:
    if settings.resolved_storage_backend == "local":
        return settings.downloads_dir.exists()
    return bool(
        settings.s3_endpoint_url
        and settings.s3_bucket_name
        and settings.s3_access_key_id
        and settings.s3_secret_access_key
    )


def redis_health_check() -> tuple[bool, str]:
    if not settings.use_celery_tasks:
        return True, "Local task registry"
    client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_socket_connect_timeout_seconds,
        health_check_interval=30,
        retry_on_timeout=False,
    )
    try:
        return bool(client.ping()), "Redis reachable"
    except RedisError as exc:
        return False, str(exc)
    finally:
        client.close()


async def collect_system_status(session: AsyncSession, include_counts: bool = False) -> dict[str, object]:
    checks: dict[str, dict[str, object]] = {}
    counts: dict[str, int] = {}

    try:
        await session.execute(text("select 1"))
        checks["database"] = {"ok": True, "label": "Connected", "detail": "Database query succeeded"}
        if include_counts:
            counts["users"] = int((await session.execute(select(func.count(User.id)))).scalar_one())
            counts["playlists"] = int((await session.execute(select(func.count(Playlist.id)))).scalar_one())
            counts["videos"] = int((await session.execute(select(func.count(Video.id)))).scalar_one())
            counts["saved_media"] = int(
                (
                    await session.execute(
                        select(func.count(PlaylistVideo.video_id)).where(PlaylistVideo.local_file_path.is_not(None))
                    )
                ).scalar_one()
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Database health check failed")
        checks["database"] = {"ok": False, "label": "Unavailable", "detail": str(exc)}

    task_ok, task_detail = await asyncio.to_thread(redis_health_check)
    checks["tasks"] = {
        "ok": task_ok,
        "label": settings.resolved_task_backend,
        "detail": task_detail,
    }

    storage_ok = storage_configured()
    checks["storage"] = {
        "ok": storage_ok,
        "label": settings.resolved_storage_backend,
        "detail": "Configured" if storage_ok else "Missing storage configuration",
    }

    checks["public_auth"] = {
        "ok": bool(settings.registration_invite_code) and settings.auth_cookie_secure_enabled,
        "label": "Hardened" if settings.registration_invite_code else "Open registration",
        "detail": "Invite code required" if settings.registration_invite_code else "Registration is open without invite code",
    }

    return {
        "environment": settings.environment,
        "app_base_url": settings.app_base_url,
        "task_backend": settings.resolved_task_backend,
        "storage_backend": settings.resolved_storage_backend,
        "email_verification_required": settings.email_verification_required,
        "invite_required": bool(settings.registration_invite_code),
        "secure_cookies": settings.auth_cookie_secure_enabled,
        "smtp_configured": bool(settings.smtp_host and settings.smtp_from_email),
        "sentry_configured": bool(settings.sentry_dsn),
        "checks": checks,
        "counts": counts,
    }


@app.get("/healthz")
async def healthz(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    system_status = await collect_system_status(session)
    checks = system_status["checks"]
    is_healthy = all(check["ok"] for check in checks.values())
    payload = {
        "status": "ok" if is_healthy else "degraded",
        "checks": {
            name: {
                "ok": check["ok"],
                "label": check["label"],
            }
            for name, check in checks.items()
        },
    }
    return JSONResponse(payload, status_code=status.HTTP_200_OK if is_healthy else status.HTTP_503_SERVICE_UNAVAILABLE)


@app.get("/login")
async def login_page(
    request: Request,
    next: str | None = None,
    current_user: User | None = Depends(get_optional_user),
):
    if current_user:
        return RedirectResponse(safe_next_path(next), status_code=status.HTTP_303_SEE_OTHER)
    return render_template(
        request,
        "login.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": None,
            "next": safe_next_path(next),
            "error": None,
            "message": None,
        },
    )


@app.post("/login")
@limiter.limit("10/minute")
async def login(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(require_csrf),
):
    form = await read_form_fields(request)
    email = form.get("email", "")
    password = form.get("password", "")
    next_path = form.get("next")
    user = await authenticate_user(session, email, password)
    if not user:
        return render_template(
            request,
            "login.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "user": None,
                "next": safe_next_path(next_path),
                "error": "Email or password is incorrect.",
                "message": None,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    response = RedirectResponse(safe_next_path(next_path), status_code=status.HTTP_303_SEE_OTHER)
    set_login_cookie(response, user)
    return response


@app.get("/register")
async def register_page(
    request: Request,
    current_user: User | None = Depends(get_optional_user),
):
    if current_user:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return render_template(
        request,
        "register.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": None,
            "error": None,
            "message": None,
            "invite_required": bool(settings.registration_invite_code),
        },
    )


@app.post("/register")
@limiter.limit("5/hour")
async def register(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(require_csrf),
):
    form = await read_form_fields(request)
    email = form.get("email", "")
    password = form.get("password", "")
    invite_code = form.get("invite_code")
    if len(password) < 8:
        return render_template(
            request,
            "register.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "user": None,
                "error": "Password must be at least 8 characters.",
                "message": None,
                "invite_required": bool(settings.registration_invite_code),
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        user = await register_user(session, email, password, invite_code)
    except HTTPException as exc:
        return render_template(
            request,
            "register.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "user": None,
                "error": str(exc.detail),
                "message": None,
                "invite_required": bool(settings.registration_invite_code),
            },
            status_code=exc.status_code,
        )

    if settings.email_verification_required and not user.is_verified:
        token = await create_email_verification_token(session, user)
        await send_verification_email(user, token)
        return render_template(
            request,
            "login.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "user": None,
                "next": "/",
                "error": None,
                "message": "Registration complete. Check your email to verify your account before logging in.",
            },
        )

    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    set_login_cookie(response, user)
    return response


@app.post("/logout")
@limiter.limit("30/minute")
async def logout(request: Request, _csrf: None = Depends(require_csrf)) -> RedirectResponse:
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_login_cookie(response)
    return response


def build_public_url(path: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}{path}"


async def send_verification_email(user: User, token: str) -> None:
    url = build_public_url(f"/verify-email?token={token}")
    await send_email(
        user.email,
        "Verify your YouTube Archive account",
        f"Verify your account by opening this link:\n\n{url}\n\nThis link expires in {settings.email_verification_token_hours} hour(s).",
    )


async def send_password_reset_email(user: User, token: str) -> None:
    url = build_public_url(f"/reset-password?token={token}")
    await send_email(
        user.email,
        "Reset your YouTube Archive password",
        f"Reset your password by opening this link:\n\n{url}\n\nThis link expires in {settings.password_reset_token_minutes} minute(s).",
    )


@app.get("/forgot-password")
async def forgot_password_page(request: Request, current_user: User | None = Depends(get_optional_user)):
    if current_user:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return render_template(
        request,
        "forgot_password.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": None,
            "error": None,
            "message": None,
        },
    )


@app.post("/forgot-password")
@limiter.limit("5/hour")
async def forgot_password(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(require_csrf),
):
    form = await read_form_fields(request)
    user = await find_user_by_email(session, form.get("email", ""))
    if user and user.hashed_password:
        token = await create_password_reset_token(session, user)
        await send_password_reset_email(user, token)

    return render_template(
        request,
        "forgot_password.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": None,
            "error": None,
            "message": "If that email exists, a reset link has been sent.",
        },
    )


@app.get("/reset-password")
async def reset_password_page(request: Request, token: str):
    return render_template(
        request,
        "reset_password.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": None,
            "token": token,
            "error": None,
        },
    )


@app.post("/reset-password")
@limiter.limit("10/hour")
async def reset_password(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _csrf: None = Depends(require_csrf),
):
    form = await read_form_fields(request)
    token = form.get("token", "")
    password = form.get("password", "")
    if len(password) < 8:
        return render_template(
            request,
            "reset_password.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "user": None,
                "token": token,
                "error": "Password must be at least 8 characters.",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    user = await reset_password_with_token(session, token, password)
    if not user:
        return render_template(
            request,
            "reset_password.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "user": None,
                "token": token,
                "error": "Reset link is invalid or expired.",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return render_template(
        request,
        "login.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": None,
            "next": "/",
            "error": None,
            "message": "Password reset complete. You can log in now.",
        },
    )


@app.get("/verify-email")
async def verify_email(request: Request, token: str, session: AsyncSession = Depends(get_session)):
    user = await verify_email_token(session, token)
    message = "Email verified. You can log in now." if user else "Verification link is invalid or expired."
    return render_template(
        request,
        "login.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": None,
            "next": "/",
            "error": None if user else message,
            "message": message if user else None,
        },
        status_code=status.HTTP_200_OK if user else status.HTTP_400_BAD_REQUEST,
    )


@app.get("/")
async def index(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User | None = Depends(get_optional_user),
):
    if not current_user:
        return login_redirect(request)
    playlists = await crud.list_playlists(session, current_user.id)
    return render_template(
        request,
        "index.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "playlists": playlists,
            "user": current_user,
        },
    )


@app.get("/settings")
async def settings_page(request: Request, current_user: User | None = Depends(get_optional_user)):
    if not current_user:
        return login_redirect(request)
    storage_percent = 0
    if current_user.storage_quota_bytes:
        storage_percent = min(100, round((current_user.storage_used_bytes / current_user.storage_quota_bytes) * 100))
    return render_template(
        request,
        "settings.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": current_user,
            "sync_interval_hours": settings.sync_interval_hours,
            "storage_percent": storage_percent,
            "playlist_quota": current_user.playlist_quota,
            "invite_required": bool(settings.registration_invite_code),
            "email_verification_required": settings.email_verification_required,
        },
    )


@app.get("/admin")
@limiter.limit("30/minute")
async def admin_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_superuser),
):
    users = await crud.list_admin_users(session)
    active_task_list = await asyncio.to_thread(list_task_statuses)
    system_status = await collect_system_status(session, include_counts=True)
    return render_template(
        request,
        "admin.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": current_user,
            "users": users,
            "active_tasks": active_task_list,
            "system_status": system_status,
        },
    )


@app.post("/admin/users/{user_id}")
@limiter.limit("30/minute")
async def admin_update_user(
    request: Request,
    user_id: str,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(require_superuser),
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    form = await read_form_fields(request)
    playlist_quota = max(0, int(form.get("playlist_quota") or 0))
    storage_quota_gb = max(0, int(form.get("storage_quota_gb") or 0))
    is_active = form.get("is_active") == "on"
    if user_id == current_user.id:
        is_active = True

    await crud.update_user_admin_settings(
        session,
        user_id,
        is_active=is_active,
        playlist_quota=playlist_quota,
        storage_quota_bytes=storage_quota_gb * 1024 * 1024 * 1024,
    )
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/playlist/{playlist_id}")
async def playlist_detail(
    playlist_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User | None = Depends(get_optional_user),
):
    if not current_user:
        return login_redirect(request)
    detail = await crud.get_playlist_detail(session, playlist_id, current_user.id)
    return render_template(
        request,
        "detail.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "playlist": detail,
            "user": current_user,
        },
    )


@app.get("/api/playlists")
@limiter.limit("60/minute")
async def api_playlists(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return await crud.list_playlists(session, current_user.id)


@app.post("/playlist/add", response_model=PlaylistAddResponse)
@limiter.limit("10/minute")
async def add_playlist(
    request: Request,
    payload: PlaylistAddRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> PlaylistAddResponse:
    try:
        metadata = await extract_playlist_metadata(payload.url)
        playlist = await crud.upsert_playlist_from_metadata(session, current_user, metadata)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to add playlist")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return PlaylistAddResponse(status="ok", message="Playlist archived", playlist_id=playlist.id)


@app.post("/playlist/{playlist_id}/sync", response_model=TaskResponse)
@limiter.limit("5/minute")
async def sync_playlist(
    request: Request,
    playlist_id: int,
    payload: SyncRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> TaskResponse:
    playlist = await crud.get_playlist_for_owner(session, playlist_id, current_user.id)
    try:
        if settings.use_celery_tasks:
            task = await asyncio.to_thread(enqueue_sync, playlist_id, current_user.id, playlist.title, payload.format)
        else:
            task = enqueue_sync(playlist_id, current_user.id, playlist.title, payload.format)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return TaskResponse(task_id=task.task_id, status=task.status, message="Sync task queued")


@app.delete("/playlist/{playlist_id}", response_model=BaseResponse)
@limiter.limit("20/minute")
async def delete_playlist(
    request: Request,
    playlist_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> BaseResponse:
    orphaned_paths = await crud.delete_playlist(session, playlist_id, current_user.id)
    message = "Playlist deleted"
    if orphaned_paths:
        message += f"; {len(orphaned_paths)} media file/object path(s) left for manual cleanup"
    return BaseResponse(status="ok", message=message)


@app.get("/stream/{video_id}")
@limiter.limit("120/minute")
async def stream_video(
    request: Request,
    video_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    media_path = await crud.get_download_path_for_video(session, current_user.id, video_id)
    media_url = await storage.get_file_url(media_path.as_posix())
    if media_url.startswith(("http://", "https://")):
        return RedirectResponse(media_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    full_path = Path(media_url)
    if not full_path.is_absolute():
        full_path = BASE_DIR / full_path
    if not full_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found on disk")
    return FileResponse(full_path)


@app.patch("/settings/sync-interval", response_model=BaseResponse)
@limiter.limit("20/minute")
async def update_sync_interval(
    request: Request,
    payload: SyncIntervalRequest,
    _current_user: User = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> BaseResponse:
    settings.sync_interval_hours = payload.hours
    if scheduler.running and scheduler.get_job("auto_sync_playlists"):
        scheduler.reschedule_job("auto_sync_playlists", trigger="interval", hours=payload.hours)
    return BaseResponse(status="ok", message=f"Auto-sync interval updated to {payload.hours} hour(s)")


@app.get("/task/{task_id}/status", response_model=TaskStatusResponse)
@limiter.limit("120/minute")
async def task_status(
    request: Request,
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> TaskStatusResponse:
    task = await asyncio.to_thread(get_task_status, task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Task does not belong to this user")
    return TaskStatusResponse(**task.to_dict())


@app.get("/tasks/active", response_model=list[TaskStatusResponse])
@limiter.limit("120/minute")
async def active_tasks(
    request: Request,
    playlist_id: int | None = None,
    current_user: User = Depends(get_current_user),
) -> list[TaskStatusResponse]:
    tasks = await asyncio.to_thread(list_task_statuses, owner_id=current_user.id, playlist_id=playlist_id)
    return [TaskStatusResponse(**task.to_dict()) for task in tasks]


@app.get("/task/{task_id}/stream")
@limiter.limit("30/minute")
async def task_stream(
    request: Request,
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    existing_task = await asyncio.to_thread(get_task_status, task_id)
    if not existing_task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if existing_task.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Task does not belong to this user")

    async def events():
        while True:
            task = await asyncio.to_thread(get_task_status, task_id)
            if not task:
                yield "event: error\ndata: {\"error\":\"Task not found\"}\n\n"
                return
            yield f"data: {json.dumps(task.to_dict())}\n\n"
            if task.status in {"done", "failed"}:
                return
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream")
