import asyncio

from celery import Celery

from config import settings
from tasks import sync_playlist_by_id


celery_app = Celery("ytarchive", broker=settings.redis_url, backend=settings.redis_url)


@celery_app.task(name="sync_playlist_task")
def sync_playlist_task(playlist_id: int, owner_id: str, format_: str = "mp3") -> str:
    asyncio.run(sync_playlist_by_id(playlist_id, owner_id, format_))
    return "done"
