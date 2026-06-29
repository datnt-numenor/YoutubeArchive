from sqlalchemy import select
from sqlalchemy.orm import selectinload

import crud
from models import Playlist, PlaylistVideo, User, Video, VideoStatus


def sample_video(video_id: str, title: str = "Song") -> dict[str, object]:
    return {
        "yt_video_id": video_id,
        "title": title,
        "channel_name": "Channel",
        "duration": 123,
        "thumbnail_url": None,
    }


async def test_deleted_video_is_not_reopened_by_metadata_sync(session) -> None:
    owner = User(email="owner@example.com")
    video = Video(**sample_video("deleted123"), status=VideoStatus.DELETED_ON_YT)
    session.add_all([owner, video])
    await session.commit()
    await session.refresh(owner)

    playlist = await crud.upsert_playlist_from_metadata(
        session,
        owner,
        {
            "yt_playlist_id": "playlist123",
            "title": "Playlist",
            "url": "https://www.youtube.com/playlist?list=playlist123",
            "videos": [sample_video("deleted123")],
        },
    )
    await session.refresh(video)

    targets = await crud.list_available_download_targets(session, playlist.id, "mp3")

    assert video.status == VideoStatus.DELETED_ON_YT
    assert targets == []


async def test_mark_unavailable_sets_deleted_status_and_error(session) -> None:
    owner = User(email="owner@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="playlist123", title="Playlist", url="https://example.com")
    video = Video(**sample_video("video123"), status=VideoStatus.AVAILABLE)
    association = PlaylistVideo(playlist=playlist, video=video)
    session.add_all([owner, playlist, video, association])
    await session.commit()

    result = await session.execute(
        select(PlaylistVideo)
        .where(PlaylistVideo.video_id == video.id)
        .options(selectinload(PlaylistVideo.video))
    )
    loaded_association = result.scalar_one()

    await crud.mark_video_unavailable_on_youtube(
        session,
        loaded_association,
        "Video unavailable. This video is not available",
    )

    assert loaded_association.video.status == VideoStatus.DELETED_ON_YT
    assert loaded_association.download_error == "Video unavailable. This video is not available"


async def test_mark_downloaded_updates_association_not_global_path(session) -> None:
    owner = User(email="owner@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="playlist123", title="Playlist", url="https://example.com")
    video = Video(**sample_video("video123"), status=VideoStatus.AVAILABLE)
    association = PlaylistVideo(playlist=playlist, video=video, download_error="old error")
    session.add_all([owner, playlist, video, association])
    await session.commit()

    await crud.mark_video_downloaded(
        session,
        association,
        "downloads/video123.mp3",
        "mp3",
        1234,
        owner,
    )

    assert association.local_file_path == "downloads/video123.mp3"
    assert association.format_saved == "mp3"
    assert association.download_error is None
    assert video.status == VideoStatus.DOWNLOADED
    assert owner.storage_used_bytes == 1234
