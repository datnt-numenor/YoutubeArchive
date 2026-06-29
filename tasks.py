import asyncio
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal

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


UNAVAILABLE_ERROR_MARKERS = (
    "video unavailable",
    "this video is not available",
    "private video",
    "video has been removed",
    "this video has been removed",
)


def is_youtube_unavailable_error(message: str) -> bool:
    normalized = message.lower()
    return any(marker in normalized for marker in UNAVAILABLE_ERROR_MARKERS)


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
    task_registry[task_id] = progress
    return progress


def get_task_status(task_id: str) -> TaskProgress | None:
    return task_registry.get(task_id)


def list_task_statuses(owner_id: str | None = None, playlist_id: int | None = None) -> list[TaskProgress]:
    tasks = list(task_registry.values())
    if owner_id is not None:
        tasks = [task for task in tasks if task.owner_id == owner_id]
    if playlist_id is not None:
        tasks = [task for task in tasks if task.playlist_id == playlist_id]
    tasks = [task for task in tasks if task.status in ACTIVE_TASK_STATES]
    return sorted(tasks, key=lambda task: task.created_at, reverse=True)[:10]


def find_active_task(playlist_id: int, owner_id: str, format_: str) -> TaskProgress | None:
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
    task = task_registry.get(task_id)
    if not task:
        task = TaskProgress(task_id=task_id, status="queued", progress=0)
        task_registry[task_id] = task

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

            object_key = await storage.upload_file(local_path, local_path.name)
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
