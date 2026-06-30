from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    import models  # noqa: F401

    if settings.environment == "production":
        return

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.database_url.startswith("sqlite"):
            # Compatibility path for local SQLite databases created before Alembic.
            result = await conn.execute(text("PRAGMA table_info(playlist_video)"))
            existing_columns = {row[1] for row in result.fetchall()}
            if "download_error" not in existing_columns:
                await conn.execute(text("ALTER TABLE playlist_video ADD COLUMN download_error TEXT"))
            if "last_download_attempt_at" not in existing_columns:
                await conn.execute(text("ALTER TABLE playlist_video ADD COLUMN last_download_attempt_at DATETIME"))

            result = await conn.execute(text("PRAGMA table_info(users)"))
            user_columns = {row[1] for row in result.fetchall()}
            if "failed_login_count" not in user_columns:
                await conn.execute(text("ALTER TABLE users ADD COLUMN failed_login_count INTEGER NOT NULL DEFAULT 0"))
            if "locked_until" not in user_columns:
                await conn.execute(text("ALTER TABLE users ADD COLUMN locked_until DATETIME"))
