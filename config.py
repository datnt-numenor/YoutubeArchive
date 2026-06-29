from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SQLITE_URL = f"sqlite+aiosqlite:///{(BASE_DIR / 'ytarchive.db').as_posix()}"


class Settings(BaseSettings):
    app_name: str = "YouTube Playlist Archiver"
    environment: Literal["development", "production"] = "development"
    secret_key: str = "change-me-in-production-min-32-chars"
    auth_cookie_name: str = "ytarchive_session"
    auth_cookie_max_age_seconds: int = 60 * 60 * 24 * 7
    auth_cookie_secure: bool | None = None
    registration_invite_code: str | None = None
    admin_emails: str = ""

    database_url: str = DEFAULT_SQLITE_URL
    redis_url: str = "redis://localhost:6379/0"

    downloads_dir: Path = BASE_DIR / "downloads"
    default_user_email: str = "local@example.com"
    default_playlist_quota: int = 10
    default_storage_quota_gb: int = 5

    sync_interval_hours: int = Field(default=12, ge=1, le=168)
    yt_sleep_min_seconds: int = Field(default=5, ge=0)
    yt_sleep_max_seconds: int = Field(default=15, ge=0)

    s3_endpoint_url: str | None = None
    s3_bucket_name: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_presigned_url_expiry: int = 3600
    sentry_dsn: str | None = None

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def storage_quota_bytes(self) -> int:
        return self.default_storage_quota_gb * 1024 * 1024 * 1024

    @property
    def auth_cookie_secure_enabled(self) -> bool:
        if self.auth_cookie_secure is not None:
            return self.auth_cookie_secure
        return self.environment == "production"

    @property
    def admin_email_set(self) -> set[str]:
        return {email.strip().lower() for email in self.admin_emails.split(",") if email.strip()}


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.downloads_dir.mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
