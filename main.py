import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

import crud
from auth import (
    authenticate_user,
    clear_login_cookie,
    get_current_user,
    get_optional_user,
    register_user,
    set_login_cookie,
)
from config import BASE_DIR, settings
from database import get_session, init_db
from downloader import extract_playlist_metadata
from models import User
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
from tasks import enqueue_local_sync, get_task_status, list_task_statuses

try:
    import sentry_sdk
except ImportError:  # pragma: no cover - optional production dependency
    sentry_sdk = None


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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


async def read_form_fields(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


@app.get("/login")
async def login_page(
    request: Request,
    next: str | None = None,
    current_user: User | None = Depends(get_optional_user),
):
    if current_user:
        return RedirectResponse(safe_next_path(next), status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": None,
            "next": safe_next_path(next),
            "error": None,
        },
    )


@app.post("/login")
async def login(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await read_form_fields(request)
    email = form.get("email", "")
    password = form.get("password", "")
    next_path = form.get("next")
    user = await authenticate_user(session, email, password)
    if not user:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "user": None,
                "next": safe_next_path(next_path),
                "error": "Email or password is incorrect.",
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
    return templates.TemplateResponse(
        request,
        "register.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": None,
            "error": None,
            "invite_required": bool(settings.registration_invite_code),
        },
    )


@app.post("/register")
async def register(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    form = await read_form_fields(request)
    email = form.get("email", "")
    password = form.get("password", "")
    invite_code = form.get("invite_code")
    if len(password) < 8:
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "user": None,
                "error": "Password must be at least 8 characters.",
                "invite_required": bool(settings.registration_invite_code),
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        user = await register_user(session, email, password, invite_code)
    except HTTPException as exc:
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "user": None,
                "error": str(exc.detail),
                "invite_required": bool(settings.registration_invite_code),
            },
            status_code=exc.status_code,
        )

    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    set_login_cookie(response, user)
    return response


@app.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_login_cookie(response)
    return response


@app.get("/")
async def index(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: User | None = Depends(get_optional_user),
):
    if not current_user:
        return login_redirect(request)
    playlists = await crud.list_playlists(session, current_user.id)
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "app_name": settings.app_name,
            "user": current_user,
            "sync_interval_hours": settings.sync_interval_hours,
        },
    )


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
    return templates.TemplateResponse(
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
async def api_playlists(
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return await crud.list_playlists(session, current_user.id)


@app.post("/playlist/add", response_model=PlaylistAddResponse)
async def add_playlist(
    payload: PlaylistAddRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
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
async def sync_playlist(
    playlist_id: int,
    payload: SyncRequest,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TaskResponse:
    playlist = await crud.get_playlist_for_owner(session, playlist_id, current_user.id)
    task = enqueue_local_sync(playlist_id, current_user.id, playlist.title, payload.format)
    return TaskResponse(task_id=task.task_id, status=task.status, message="Sync task queued")


@app.delete("/playlist/{playlist_id}", response_model=BaseResponse)
async def delete_playlist(
    playlist_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> BaseResponse:
    orphaned_paths = await crud.delete_playlist(session, playlist_id, current_user.id)
    message = "Playlist deleted"
    if orphaned_paths:
        message += f"; {len(orphaned_paths)} media file/object path(s) left for manual cleanup"
    return BaseResponse(status="ok", message=message)


@app.get("/stream/{video_id}")
async def stream_video(
    video_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    media_path = await crud.get_download_path_for_video(session, current_user.id, video_id)
    full_path = media_path if media_path.is_absolute() else BASE_DIR / media_path
    if not full_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found on disk")
    return FileResponse(full_path)


@app.patch("/settings/sync-interval", response_model=BaseResponse)
async def update_sync_interval(
    payload: SyncIntervalRequest,
    _current_user: User = Depends(get_current_user),
) -> BaseResponse:
    settings.sync_interval_hours = payload.hours
    if scheduler.running and scheduler.get_job("auto_sync_playlists"):
        scheduler.reschedule_job("auto_sync_playlists", trigger="interval", hours=payload.hours)
    return BaseResponse(status="ok", message=f"Auto-sync interval updated to {payload.hours} hour(s)")


@app.get("/task/{task_id}/status", response_model=TaskStatusResponse)
async def task_status(
    task_id: str,
    current_user: User = Depends(get_current_user),
) -> TaskStatusResponse:
    task = get_task_status(task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Task does not belong to this user")
    return TaskStatusResponse(**task.to_dict())


@app.get("/tasks/active", response_model=list[TaskStatusResponse])
async def active_tasks(
    playlist_id: int | None = None,
    current_user: User = Depends(get_current_user),
) -> list[TaskStatusResponse]:
    tasks = list_task_statuses(owner_id=current_user.id, playlist_id=playlist_id)
    return [TaskStatusResponse(**task.to_dict()) for task in tasks]


@app.get("/task/{task_id}/stream")
async def task_stream(
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    existing_task = get_task_status(task_id)
    if not existing_task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if existing_task.owner_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Task does not belong to this user")

    async def events():
        while True:
            task = get_task_status(task_id)
            if not task:
                yield "event: error\ndata: {\"error\":\"Task not found\"}\n\n"
                return
            yield f"data: {json.dumps(task.to_dict())}\n\n"
            if task.status in {"done", "failed"}:
                return
            await asyncio.sleep(1)

    return StreamingResponse(events(), media_type="text/event-stream")
