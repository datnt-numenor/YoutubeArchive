import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

try:
    from redis import Redis
    from redis.exceptions import RedisError
except ImportError:  # pragma: no cover - runtime dependency is declared in requirements
    Redis = None  # type: ignore[assignment]
    RedisError = Exception  # type: ignore[assignment]

try:
    import sentry_sdk
except ImportError:  # pragma: no cover - optional production dependency
    sentry_sdk = None

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import crud
from config import settings
from database import AsyncSessionLocal
from downloader import download_video, extract_playlist_metadata
from models import Playlist, PlaylistVideo, User
from storage import storage

logger = logging.getLogger(__name__)

TaskState = Literal["queued", "running", "done", "failed"]
ACTIVE_TASK_STATES = {"queued", "running"}
METADATA_IMPORT_FORMAT = "metadata"


@dataclass
class TaskVideoError:
    video_id: int
    yt_video_id: str
    title: str
    message: str


@dataclass
class TaskProgress:
    task_id: str
    status: TaskState
    playlist_id: int | None = None
    playlist_title: str | None = None
    owner_id: str | None = None
    format: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    progress: int | None = None
    error: str | None = None
    total: int = 0
    completed: int = 0
    failed: int = 0
    current_video: str | None = None
    errors: list[TaskVideoError] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


task_registry: dict[str, TaskProgress] = {}
_redis: Any | None = None

TASK_KEY_PREFIX = "ytarchive:task:"
OWNER_TASKS_KEY_PREFIX = "ytarchive:owner_tasks:"
ACTIVE_TASK_KEY_PREFIX = "ytarchive:active_task:"


UNAVAILABLE_ERROR_MARKERS = (
    "video unavailable",
    "this video is not available",
    "private video",
    "video has been removed",
    "this video has been removed",
)


def build_media_object_key(owner_id: str, playlist_id: int, filename: str) -> str:
    safe_filename = filename.replace("\\", "/").split("/")[-1]
    return f"users/{owner_id}/playlists/{playlist_id}/{safe_filename}"


def is_youtube_unavailable_error(message: str) -> bool:
    normalized = message.lower()
    return any(marker in normalized for marker in UNAVAILABLE_ERROR_MARKERS)


def _redis_client() -> Any | None:
    if not settings.use_celery_tasks:
        return None
    if Redis is None:
        raise RuntimeError("Redis package is not installed")

    global _redis
    if _redis is None:
        _redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=settings.redis_socket_timeout_seconds,
            socket_connect_timeout=settings.redis_socket_connect_timeout_seconds,
            health_check_interval=30,
            retry_on_timeout=False,
        )
    return _redis


def _task_key(task_id: str) -> str:
    return f"{TASK_KEY_PREFIX}{task_id}"


def _owner_tasks_key(owner_id: str) -> str:
    return f"{OWNER_TASKS_KEY_PREFIX}{owner_id}"


def _active_task_key(playlist_id: int, owner_id: str, format_: str) -> str:
    return f"{ACTIVE_TASK_KEY_PREFIX}{owner_id}:{playlist_id}:{format_}"


def _coerce_task(raw: dict[str, Any]) -> TaskProgress:
    fields = TaskProgress.__dataclass_fields__
    data = {key: raw[key] for key in fields if key in raw}
    data["errors"] = [
        error if isinstance(error, TaskVideoError) else TaskVideoError(**error)
        for error in raw.get("errors", [])
    ]
    return TaskProgress(**data)


def _load_redis_task(task_id: str) -> TaskProgress | None:
    client = _redis_client()
    if client is None:
        return None

    try:
        payload = client.get(_task_key(task_id))
    except RedisError:
        logger.warning("Unable to load task status from Redis", exc_info=True)
        return None
    if not payload:
        return None
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    return _coerce_task(json.loads(payload))


def _save_redis_task(task: TaskProgress) -> None:
    client = _redis_client()
    if client is None:
        return

    ttl = settings.task_status_ttl_seconds
    client.setex(_task_key(task.task_id), ttl, json.dumps(task.to_dict()))
    if task.owner_id:
        owner_key = _owner_tasks_key(task.owner_id)
        client.sadd(owner_key, task.task_id)
        client.expire(owner_key, ttl)

    if task.playlist_id is None or not task.owner_id or not task.format:
        return

    active_key = _active_task_key(task.playlist_id, task.owner_id, task.format)
    if task.status in ACTIVE_TASK_STATES:
        client.setex(active_key, ttl, task.task_id)
        return

    active_task_id = client.get(active_key)
    if isinstance(active_task_id, bytes):
        active_task_id = active_task_id.decode("utf-8")
    if active_task_id == task.task_id:
        client.delete(active_key)


def _save_task(task: TaskProgress) -> None:
    if settings.use_celery_tasks:
        _save_redis_task(task)
    else:
        task_registry[task.task_id] = task


def create_task_status(playlist_id: int, owner_id: str, playlist_title: str, format_: str) -> TaskProgress:
    task_id = str(uuid.uuid4())
    progress = TaskProgress(
        task_id=task_id,
        status="queued",
        playlist_id=playlist_id,
        playlist_title=playlist_title,
        owner_id=owner_id,
        format=format_,
        progress=0,
    )
    _save_task(progress)
    return progress


def get_task_status(task_id: str) -> TaskProgress | None:
    if settings.use_celery_tasks:
        return _load_redis_task(task_id)
    return task_registry.get(task_id)


def list_task_statuses(owner_id: str | None = None, playlist_id: int | None = None) -> list[TaskProgress]:
    if settings.use_celery_tasks:
        client = _redis_client()
        if client is None:
            return []
        try:
            task_ids: set[str] = set()
            if owner_id is not None:
                raw_task_ids = client.smembers(_owner_tasks_key(owner_id))
                task_ids = {task_id.decode("utf-8") if isinstance(task_id, bytes) else task_id for task_id in raw_task_ids}
            else:
                for key in client.scan_iter(f"{TASK_KEY_PREFIX}*"):
                    key_text = key.decode("utf-8") if isinstance(key, bytes) else key
                    task_ids.add(key_text.removeprefix(TASK_KEY_PREFIX))
            tasks = [task for task_id in task_ids if (task := _load_redis_task(task_id))]
        except RedisError:
            logger.warning("Unable to list active task statuses from Redis", exc_info=True)
            return []
    else:
        tasks = list(task_registry.values())

    if owner_id is not None:
        tasks = [task for task in tasks if task.owner_id == owner_id]
    if playlist_id is not None:
        tasks = [task for task in tasks if task.playlist_id == playlist_id]
    tasks = [task for task in tasks if task.status in ACTIVE_TASK_STATES]
    return sorted(tasks, key=lambda task: task.created_at, reverse=True)[:10]


def find_active_task(playlist_id: int, owner_id: str, format_: str) -> TaskProgress | None:
    if settings.use_celery_tasks:
        client = _redis_client()
        if client is None:
            return None

        try:
            active_key = _active_task_key(playlist_id, owner_id, format_)
            task_id = client.get(active_key)
            if isinstance(task_id, bytes):
                task_id = task_id.decode("utf-8")
            if task_id:
                task = _load_redis_task(task_id)
                if task and task.status in ACTIVE_TASK_STATES:
                    return task
                client.delete(active_key)
        except RedisError:
            logger.warning("Unable to find active task in Redis", exc_info=True)
            return None

        for task in list_task_statuses(owner_id=owner_id, playlist_id=playlist_id):
            if task.format == format_ and task.status in ACTIVE_TASK_STATES:
                return task
        return None

    for task in task_registry.values():
        if (
            task.playlist_id == playlist_id
            and task.owner_id == owner_id
            and task.format == format_
            and task.status in ACTIVE_TASK_STATES
        ):
            return task
    return None


def _update_task(
    task_id: str,
    *,
    status: TaskState | None = None,
    progress: int | None = None,
    error: str | None = None,
    total: int | None = None,
    completed: int | None = None,
    failed: int | None = None,
    current_video: str | None = None,
    append_error: TaskVideoError | None = None,
) -> None:
    task = get_task_status(task_id)
    if not task:
        task = TaskProgress(task_id=task_id, status="queued", progress=0)

    task.updated_at = datetime.now(timezone.utc).isoformat()
    if status is not None:
        task.status = status
        if status in {"done", "failed"} and task.finished_at is None:
            task.finished_at = task.updated_at
    if progress is not None:
        task.progress = progress
    if error is not None:
        task.error = error
    if total is not None:
        task.total = total
    if completed is not None:
        task.completed = completed
    if failed is not None:
        task.failed = failed
    if current_video is not None:
        task.current_video = current_video
    if append_error is not None:
        task.errors.append(append_error)

    _save_task(task)


async def sync_playlist(session: AsyncSession, playlist: Playlist, owner: User, format_: str, task_id: str | None = None) -> None:
    if task_id:
        _update_task(task_id, status="running", progress=3, current_video="Reading playlist metadata")

    metadata = await extract_playlist_metadata(playlist.url)
    playlist = await crud.sync_playlist_metadata(session, playlist, metadata)
    targets = await crud.list_available_download_targets(session, playlist.id, format_)

    if not targets:
        if task_id:
            _update_task(task_id, status="done", progress=100, total=0, current_video="Nothing new to download")
        return

    total = len(targets)
    completed = 0
    failed = 0
    if task_id:
        _update_task(task_id, status="running", progress=5, total=total, completed=0, failed=0)

    for index, association in enumerate(targets, start=1):
        video_title = association.video.title
        try:
            if owner.storage_used_bytes >= owner.storage_quota_bytes:
                message = "Storage quota reached"
                logger.warning("%s for user %s", message, owner.email)
                await crud.mark_video_download_failed(session, association, message)
                failed += 1
                if task_id:
                    _update_task(
                        task_id,
                        failed=failed,
                        append_error=TaskVideoError(
                            video_id=association.video.id,
                            yt_video_id=association.video.yt_video_id,
                            title=video_title,
                            message=message,
                        ),
                    )
                break

            if task_id:
                _update_task(task_id, current_video=video_title)

            await crud.mark_video_download_started(session, association)
            local_path = await download_video(
                association.video.yt_video_id,
                settings.downloads_dir,
                format_,  # type: ignore[arg-type]
            )
            file_size = local_path.stat().st_size
            if owner.storage_used_bytes + file_size > owner.storage_quota_bytes:
                message = "This file would exceed storage quota"
                logger.warning("Skipping %s because it would exceed user storage quota", local_path)
                await crud.mark_video_download_failed(session, association, message)
                failed += 1
                if task_id:
                    _update_task(
                        task_id,
                        failed=failed,
                        append_error=TaskVideoError(
                            video_id=association.video.id,
                            yt_video_id=association.video.yt_video_id,
                            title=video_title,
                            message=message,
                        ),
                    )
                continue

            object_key = await storage.upload_file(
                local_path,
                build_media_object_key(owner.id, association.playlist_id, local_path.name),
            )
            await crud.mark_video_downloaded(session, association, object_key, format_, file_size, owner)
            completed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            message = str(exc) or exc.__class__.__name__
            logger.exception("Failed to download video %s", association.video.yt_video_id)
            if is_youtube_unavailable_error(message):
                await crud.mark_video_unavailable_on_youtube(session, association, message)
            else:
                await crud.mark_video_download_failed(session, association, message)
            if sentry_sdk:
                sentry_sdk.capture_exception(exc)
            if task_id:
                _update_task(
                    task_id,
                    failed=failed,
                    append_error=TaskVideoError(
                        video_id=association.video.id,
                        yt_video_id=association.video.yt_video_id,
                        title=video_title,
                        message=message,
                    ),
                )

        if task_id:
            _update_task(
                task_id,
                status="running",
                progress=5 + int(index / total * 90),
                completed=completed,
                failed=failed,
            )

    if task_id:
        _update_task(task_id, status="done", progress=100, completed=completed, failed=failed, current_video="Finished")


async def import_playlist_metadata(session: AsyncSession, playlist: Playlist, task_id: str | None = None) -> None:
    if task_id:
        _update_task(task_id, status="running", progress=5, current_video="Reading playlist metadata")

    metadata = await extract_playlist_metadata(playlist.url)
    await crud.sync_playlist_metadata(session, playlist, metadata)
    video_count = len(metadata.get("videos") or [])
    if task_id:
        _update_task(
            task_id,
            status="done",
            progress=100,
            total=video_count,
            completed=video_count,
            failed=0,
            current_video="Metadata imported",
        )


async def import_playlist_metadata_by_id(playlist_id: int, owner_id: str, task_id: str | None = None) -> None:
    async with AsyncSessionLocal() as session:
        playlist = await crud.get_playlist_for_owner(session, playlist_id, owner_id)
        await import_playlist_metadata(session, playlist, task_id)


async def sync_playlist_by_id(playlist_id: int, owner_id: str | None = None, format_: str = "mp3", task_id: str | None = None) -> None:
    async with AsyncSessionLocal() as session:
        if owner_id:
            playlist = await crud.get_playlist_for_owner(session, playlist_id, owner_id)
        else:
            result = await session.execute(
                select(Playlist)
                .where(Playlist.id == playlist_id)
                .options(selectinload(Playlist.playlist_videos).selectinload(PlaylistVideo.video))
            )
            playlist = result.scalar_one_or_none()
            if not playlist:
                raise ValueError(f"Playlist {playlist_id} does not exist")
        owner = await session.get(User, playlist.owner_id)
        if not owner:
            raise ValueError(f"Owner {playlist.owner_id} does not exist")
        await sync_playlist(session, playlist, owner, format_, task_id)


def enqueue_local_sync(playlist_id: int, owner_id: str, playlist_title: str, format_: str) -> TaskProgress:
    existing = find_active_task(playlist_id, owner_id, format_)
    if existing:
        return existing

    progress = create_task_status(playlist_id, owner_id, playlist_title, format_)

    async def runner() -> None:
        try:
            await sync_playlist_by_id(playlist_id, owner_id, format_, progress.task_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Sync task failed")
            _update_task(progress.task_id, status="failed", error=str(exc))

    asyncio.create_task(runner())
    return progress


def enqueue_local_metadata_import(playlist_id: int, owner_id: str, playlist_title: str) -> TaskProgress:
    existing = find_active_task(playlist_id, owner_id, METADATA_IMPORT_FORMAT)
    if existing:
        return existing

    progress = create_task_status(playlist_id, owner_id, playlist_title, METADATA_IMPORT_FORMAT)

    async def runner() -> None:
        try:
            await import_playlist_metadata_by_id(playlist_id, owner_id, progress.task_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Metadata import task failed")
            _update_task(progress.task_id, status="failed", error=str(exc))

    asyncio.create_task(runner())
    return progress


def _send_celery_sync_task(playlist_id: int, owner_id: str, format_: str, task_id: str) -> None:
    from worker import sync_playlist_task

    sync_playlist_task.apply_async(
        args=[playlist_id, owner_id, format_, task_id],
        task_id=task_id,
    )


def _send_celery_metadata_import_task(playlist_id: int, owner_id: str, task_id: str) -> None:
    from worker import import_playlist_metadata_task

    import_playlist_metadata_task.apply_async(
        args=[playlist_id, owner_id, task_id],
        task_id=task_id,
    )


def enqueue_celery_sync(playlist_id: int, owner_id: str, playlist_title: str, format_: str) -> TaskProgress:
    progress: TaskProgress | None = None
    try:
        existing = find_active_task(playlist_id, owner_id, format_)
        if existing:
            return existing

        progress = create_task_status(playlist_id, owner_id, playlist_title, format_)
        _send_celery_sync_task(playlist_id, owner_id, format_, progress.task_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to enqueue Celery sync task")
        if progress:
            try:
                _update_task(progress.task_id, status="failed", error=str(exc) or exc.__class__.__name__)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to mark Celery sync task as failed")
        raise RuntimeError("Unable to enqueue sync task with Celery/Redis") from exc
    return progress


def enqueue_celery_metadata_import(playlist_id: int, owner_id: str, playlist_title: str) -> TaskProgress:
    progress: TaskProgress | None = None
    try:
        existing = find_active_task(playlist_id, owner_id, METADATA_IMPORT_FORMAT)
        if existing:
            return existing

        progress = create_task_status(playlist_id, owner_id, playlist_title, METADATA_IMPORT_FORMAT)
        _send_celery_metadata_import_task(playlist_id, owner_id, progress.task_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to enqueue Celery metadata import task")
        if progress:
            try:
                _update_task(progress.task_id, status="failed", error=str(exc) or exc.__class__.__name__)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to mark Celery metadata import task as failed")
        raise RuntimeError("Unable to enqueue metadata import task with Celery/Redis") from exc
    return progress


def enqueue_sync(playlist_id: int, owner_id: str, playlist_title: str, format_: str) -> TaskProgress:
    if settings.use_celery_tasks:
        return enqueue_celery_sync(playlist_id, owner_id, playlist_title, format_)
    return enqueue_local_sync(playlist_id, owner_id, playlist_title, format_)


def enqueue_metadata_import(playlist_id: int, owner_id: str, playlist_title: str) -> TaskProgress:
    if settings.use_celery_tasks:
        return enqueue_celery_metadata_import(playlist_id, owner_id, playlist_title)
    return enqueue_local_metadata_import(playlist_id, owner_id, playlist_title)
