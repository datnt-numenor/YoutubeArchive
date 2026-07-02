from types import SimpleNamespace

from main import active_tasks, collect_system_status
from models import Playlist, PlaylistVideo, User, Video, VideoStatus
import tasks


def setup_function() -> None:
    tasks.task_registry.clear()


async def test_active_tasks_endpoint_logic_returns_only_current_user_tasks() -> None:
    current = tasks.create_task_status(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")
    tasks.create_task_status(playlist_id=2, owner_id="owner-2", playlist_title="Other", format_="mp3")

    response = await active_tasks.__wrapped__(request=None, current_user=SimpleNamespace(id="owner-1"))

    assert len(response) == 1
    assert response[0].task_id == current.task_id
    assert response[0].playlist_id == 1


async def test_collect_system_status_reports_counts(session, monkeypatch) -> None:
    monkeypatch.setattr("main.settings.task_backend", "local")
    monkeypatch.setattr("main.settings.storage_backend", "local")
    monkeypatch.setattr("main.settings.registration_invite_code", "invite")
    monkeypatch.setattr("main.settings.auth_cookie_secure", True)

    owner = User(id="owner-1", email="owner@example.com")
    playlist = Playlist(id=1, owner_id=owner.id, yt_playlist_id="PL123", title="Piano", url="https://example.test")
    video = Video(id=1, yt_video_id="abc123", title="Song", channel_name="Channel", status=VideoStatus.DOWNLOADED)
    association = PlaylistVideo(playlist_id=playlist.id, video_id=video.id, local_file_path="downloads/song.mp3")
    session.add_all([owner, playlist, video, association])
    await session.commit()

    status = await collect_system_status(session, include_counts=True)

    assert status["checks"]["database"]["ok"]
    assert status["checks"]["tasks"]["label"] == "local"
    assert status["checks"]["public_auth"]["ok"]
    assert status["counts"] == {
        "users": 1,
        "playlists": 1,
        "videos": 1,
        "saved_media": 1,
    }
