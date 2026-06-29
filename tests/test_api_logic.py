from types import SimpleNamespace

from main import active_tasks
import tasks


def setup_function() -> None:
    tasks.task_registry.clear()


async def test_active_tasks_endpoint_logic_returns_only_current_user_tasks() -> None:
    current = tasks.create_task_status(playlist_id=1, owner_id="owner-1", playlist_title="Piano", format_="mp3")
    tasks.create_task_status(playlist_id=2, owner_id="owner-2", playlist_title="Other", format_="mp3")

    response = await active_tasks(current_user=SimpleNamespace(id="owner-1"))

    assert len(response) == 1
    assert response[0].task_id == current.task_id
    assert response[0].playlist_id == 1
