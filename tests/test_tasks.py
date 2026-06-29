import tasks


def setup_function() -> None:
    tasks.task_registry.clear()


def test_unavailable_error_detection() -> None:
    assert tasks.is_youtube_unavailable_error("ERROR: Video unavailable. This video is not available")
    assert tasks.is_youtube_unavailable_error("Private video")
    assert not tasks.is_youtube_unavailable_error("HTTP Error 416: Requested range not satisfiable")


def test_active_task_listing_hides_finished_tasks() -> None:
    active = tasks.create_task_status(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")
    finished = tasks.create_task_status(playlist_id=2, owner_id="owner-1", playlist_title="Old", format_="mp3")
    tasks._update_task(finished.task_id, status="done", progress=100)

    listed = tasks.list_task_statuses(owner_id="owner-1")

    assert [task.task_id for task in listed] == [active.task_id]
    assert tasks.find_active_task(1, "owner-1", "mp3") == active
    assert tasks.find_active_task(2, "owner-1", "mp3") is None


def test_enqueue_sync_reuses_existing_active_task(monkeypatch) -> None:
    created_coroutines = []

    def fake_create_task(coro):
        created_coroutines.append(coro)
        coro.close()
        return object()

    monkeypatch.setattr(tasks.asyncio, "create_task", fake_create_task)

    first = tasks.enqueue_local_sync(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")
    second = tasks.enqueue_local_sync(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")

    assert first is second
    assert len(created_coroutines) == 1
