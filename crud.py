import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models import Playlist, PlaylistVideo, User, Video, VideoStatus
from schemas import PlaylistDetailResponse, PlaylistSchema, VideoSchema

logger = logging.getLogger(__name__)


async def count_user_playlists(session: AsyncSession, owner_id: str) -> int:
    result = await session.execute(select(func.count(Playlist.id)).where(Playlist.owner_id == owner_id))
    return int(result.scalar_one())


async def list_playlists(session: AsyncSession, owner_id: str) -> list[PlaylistSchema]:
    result = await session.execute(
        select(Playlist)
        .where(Playlist.owner_id == owner_id)
        .options(selectinload(Playlist.playlist_videos))
        .order_by(Playlist.created_at.desc())
    )
    playlists = result.scalars().unique().all()
    return [
        PlaylistSchema(
            id=playlist.id,
            title=playlist.title,
            url=playlist.url,
            auto_sync=playlist.auto_sync,
            created_at=playlist.created_at,
            last_synced=playlist.last_synced,
            video_count=len(playlist.playlist_videos),
        )
        for playlist in playlists
    ]


async def get_playlist_for_owner(session: AsyncSession, playlist_id: int, owner_id: str) -> Playlist:
    result = await session.execute(
        select(Playlist)
        .where(Playlist.id == playlist_id, Playlist.owner_id == owner_id)
        .options(selectinload(Playlist.playlist_videos).selectinload(PlaylistVideo.video))
    )
    playlist = result.scalar_one_or_none()
    if not playlist:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Playlist does not belong to this user")
    return playlist


async def _get_video_by_youtube_id(session: AsyncSession, yt_video_id: str) -> Video | None:
    result = await session.execute(select(Video).where(Video.yt_video_id == yt_video_id))
    return result.scalar_one_or_none()


async def _get_playlist_video(session: AsyncSession, playlist_id: int, video_id: int) -> PlaylistVideo | None:
    result = await session.execute(
        select(PlaylistVideo).where(
            PlaylistVideo.playlist_id == playlist_id,
            PlaylistVideo.video_id == video_id,
        )
    )
    return result.scalar_one_or_none()


def _apply_video_metadata(video: Video, metadata: dict[str, Any]) -> None:
    video.title = metadata["title"]
    video.channel_name = metadata.get("channel_name") or ""
    video.duration = int(metadata.get("duration") or 0)
    video.thumbnail_url = metadata.get("thumbnail_url")


async def upsert_playlist_from_metadata(session: AsyncSession, owner: User, metadata: dict[str, Any]) -> Playlist:
    result = await session.execute(
        select(Playlist).where(
            Playlist.owner_id == owner.id,
            Playlist.yt_playlist_id == metadata["yt_playlist_id"],
        )
    )
    playlist = result.scalar_one_or_none()
    if not playlist:
        total = await count_user_playlists(session, owner.id)
        if total >= owner.playlist_quota:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Playlist quota exceeded")
        playlist = Playlist(
            owner_id=owner.id,
            yt_playlist_id=metadata["yt_playlist_id"],
            title=metadata["title"],
            url=metadata["url"],
        )
        session.add(playlist)
        await session.flush()
    else:
        playlist.title = metadata["title"]
        playlist.url = metadata["url"]

    for video_data in metadata["videos"]:
        video = await _get_video_by_youtube_id(session, video_data["yt_video_id"])
        if not video:
            video = Video(**video_data, status=VideoStatus.AVAILABLE)
            session.add(video)
            await session.flush()
        else:
            _apply_video_metadata(video, video_data)

        association = await _get_playlist_video(session, playlist.id, video.id)
        if not association:
            session.add(PlaylistVideo(playlist_id=playlist.id, video_id=video.id))

    await session.commit()
    await session.refresh(playlist)
    return playlist


async def sync_playlist_metadata(session: AsyncSession, playlist: Playlist, metadata: dict[str, Any]) -> Playlist:
    current_yt_ids = {video["yt_video_id"] for video in metadata["videos"]}
    playlist.title = metadata["title"]
    playlist.url = metadata["url"]
    playlist.last_synced = datetime.now(timezone.utc)

    for association in playlist.playlist_videos:
        if association.video.yt_video_id not in current_yt_ids:
            association.video.status = VideoStatus.DELETED_ON_YT

    for video_data in metadata["videos"]:
        video = await _get_video_by_youtube_id(session, video_data["yt_video_id"])
        if not video:
            video = Video(**video_data, status=VideoStatus.AVAILABLE)
            session.add(video)
            await session.flush()
        else:
            _apply_video_metadata(video, video_data)

        association = await _get_playlist_video(session, playlist.id, video.id)
        if not association:
            session.add(PlaylistVideo(playlist_id=playlist.id, video_id=video.id))

    await session.commit()
    await session.refresh(playlist)
    return playlist


async def get_playlist_detail(session: AsyncSession, playlist_id: int, owner_id: str) -> PlaylistDetailResponse:
    playlist = await get_playlist_for_owner(session, playlist_id, owner_id)
    videos = [
        VideoSchema(
            id=association.video.id,
            yt_video_id=association.video.yt_video_id,
            title=association.video.title,
            channel_name=association.video.channel_name,
            duration=association.video.duration,
            thumbnail_url=association.video.thumbnail_url,
            status=association.video.status.value,
            local_file_path=association.local_file_path,
            format_saved=association.format_saved,
            download_error=association.download_error,
            last_download_attempt_at=association.last_download_attempt_at,
        )
        for association in playlist.playlist_videos
    ]
    return PlaylistDetailResponse(
        id=playlist.id,
        title=playlist.title,
        url=playlist.url,
        auto_sync=playlist.auto_sync,
        last_synced=playlist.last_synced,
        videos=videos,
    )


async def list_available_download_targets(session: AsyncSession, playlist_id: int, format_saved: str) -> list[PlaylistVideo]:
    result = await session.execute(
        select(PlaylistVideo)
        .where(PlaylistVideo.playlist_id == playlist_id)
        .join(PlaylistVideo.video)
        .where(Video.status != VideoStatus.DELETED_ON_YT)
        .where(
            (PlaylistVideo.local_file_path.is_(None))
            | (PlaylistVideo.format_saved.is_(None))
            | (PlaylistVideo.format_saved != format_saved)
        )
        .options(selectinload(PlaylistVideo.video))
        .order_by(PlaylistVideo.added_at.desc())
    )
    return list(result.scalars().unique().all())


async def mark_video_downloaded(
    session: AsyncSession,
    association: PlaylistVideo,
    object_key: str,
    format_saved: str,
    file_size: int,
    owner: User,
) -> None:
    association.local_file_path = object_key
    association.format_saved = format_saved
    association.download_error = None
    association.last_download_attempt_at = datetime.now(timezone.utc)
    association.video.status = VideoStatus.DOWNLOADED
    owner.storage_used_bytes += file_size
    await session.commit()


async def mark_video_download_started(session: AsyncSession, association: PlaylistVideo) -> None:
    association.download_error = None
    association.last_download_attempt_at = datetime.now(timezone.utc)
    await session.commit()


async def mark_video_download_failed(session: AsyncSession, association: PlaylistVideo, error: str) -> None:
    association.download_error = error[:2000]
    association.last_download_attempt_at = datetime.now(timezone.utc)
    await session.commit()


async def mark_video_unavailable_on_youtube(session: AsyncSession, association: PlaylistVideo, error: str) -> None:
    association.download_error = error[:2000]
    association.last_download_attempt_at = datetime.now(timezone.utc)
    association.video.status = VideoStatus.DELETED_ON_YT
    await session.commit()


async def delete_playlist(session: AsyncSession, playlist_id: int, owner_id: str) -> list[str]:
    playlist = await get_playlist_for_owner(session, playlist_id, owner_id)
    orphaned_paths = [
        association.local_file_path
        for association in playlist.playlist_videos
        if association.local_file_path
    ]
    for path in orphaned_paths:
        logger.warning("Playlist deletion leaves media file/object for manual cleanup: %s", path)

    await session.delete(playlist)
    await session.commit()
    return orphaned_paths


async def set_playlist_auto_sync(session: AsyncSession, playlist_id: int, owner_id: str, auto_sync: bool) -> Playlist:
    playlist = await get_playlist_for_owner(session, playlist_id, owner_id)
    playlist.auto_sync = auto_sync
    await session.commit()
    await session.refresh(playlist)
    return playlist


async def list_auto_sync_playlist_ids(session: AsyncSession) -> list[int]:
    result = await session.execute(select(Playlist.id).where(Playlist.auto_sync.is_(True)))
    return list(result.scalars().all())


async def get_download_path_for_video(session: AsyncSession, owner_id: str, video_id: int) -> Path:
    result = await session.execute(
        select(PlaylistVideo)
        .join(PlaylistVideo.playlist)
        .where(
            Playlist.owner_id == owner_id,
            PlaylistVideo.video_id == video_id,
            PlaylistVideo.local_file_path.is_not(None),
        )
        .limit(1)
    )
    association = result.scalar_one_or_none()
    if not association or not association.local_file_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Downloaded media not found")
    return Path(association.local_file_path)
