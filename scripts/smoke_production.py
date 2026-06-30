import argparse
import asyncio
from pathlib import Path

from sqlalchemy import text

from config import settings
from database import engine


async def check_database() -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    print("ok database")


def check_redis() -> None:
    from redis import Redis

    client = Redis.from_url(settings.redis_url, decode_responses=True)
    if client.ping() is not True:
        raise RuntimeError("Redis ping failed")
    print("ok redis")


def check_celery_workers() -> None:
    from worker import celery_app

    replies = celery_app.control.ping(timeout=3)
    if not replies:
        raise RuntimeError("No Celery workers replied to ping")
    print(f"ok celery workers: {len(replies)}")


async def check_s3(write_object: bool) -> None:
    from storage import S3Storage

    storage = S3Storage()
    test_key = "smoke/ytarchive-smoke.txt"
    if write_object:
        local_path = Path("ytarchive-smoke.txt")
        local_path.write_text("ytarchive smoke test\n", encoding="utf-8")
        try:
            await storage.upload_file(local_path, test_key)
        finally:
            local_path.unlink(missing_ok=True)

    url = await storage.get_file_url(test_key)
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("S3 presigned URL was not generated")
    print("ok s3 presigned url")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke test production services.")
    parser.add_argument("--skip-celery", action="store_true", help="Skip Celery worker ping.")
    parser.add_argument("--skip-s3", action="store_true", help="Skip S3/R2 presigned URL check.")
    parser.add_argument("--s3-write", action="store_true", help="Upload a tiny smoke object before generating URL.")
    args = parser.parse_args()

    await check_database()
    check_redis()
    if settings.use_celery_tasks and not args.skip_celery:
        check_celery_workers()
    if settings.resolved_storage_backend == "s3" and not args.skip_s3:
        await check_s3(args.s3_write)


if __name__ == "__main__":
    asyncio.run(main())
