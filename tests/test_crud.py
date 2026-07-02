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


async def test_list_playlists_counts_videos_without_loading_detail_rows(session) -> None:
    owner = User(email="owner@example.com")
    other_owner = User(email="other@example.com")
    playlist = Playlist(owner=owner, yt_playlist_id="playlist123", title="Playlist", url="https://example.com")
    other_playlist = Playlist(owner=other_owner, yt_playlist_id="playlist456", title="Other", url="https://example.com")
    first_video = Video(**sample_video("video123"), status=VideoStatus.AVAILABLE)
    second_video = Video(**sample_video("video456"), status=VideoStatus.AVAILABLE)
    session.add_all(
        [
            owner,
            other_owner,
            playlist,
            other_playlist,
            first_video,
            second_video,
            PlaylistVideo(playlist=playlist, video=first_video),
            PlaylistVideo(playlist=playlist, video=second_video),
            PlaylistVideo(playlist=other_playlist, video=second_video),
        ]
    )
    await session.commit()

    playlists = await crud.list_playlists(session, owner.id)

    assert len(playlists) == 1
    assert playlists[0].id == playlist.id
    assert playlists[0].video_count == 2


async def test_get_or_create_pending_playlist_returns_quick_placeholder(session) -> None:
    owner = User(email="owner@example.com", playlist_quota=10)
    session.add(owner)
    await session.commit()
    await session.refresh(owner)

    playlist, created = await crud.get_or_create_pending_playlist(
        session,
        owner,
        yt_playlist_id="PL_FAST_ADD",
        url="https://www.youtube.com/playlist?list=PL_FAST_ADD",
    )
    existing, created_again = await crud.get_or_create_pending_playlist(
        session,
        owner,
        yt_playlist_id="PL_FAST_ADD",
        url="https://www.youtube.com/playlist?list=PL_FAST_ADD",
    )

    assert created is True
    assert playlist.id == existing.id
    assert created_again is False
    assert playlist.title == "Importing playlist PL_FAST_ADD"


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
