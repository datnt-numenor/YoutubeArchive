# YouTube Playlist Archiver

FastAPI app for archiving YouTube playlist metadata and downloading playlist media locally with `yt-dlp`.

## What is included

- FastAPI + Jinja2 UI
- Async SQLAlchemy + SQLite default database
- Pydantic v2 schemas
- `yt-dlp` metadata extraction and MP3/MP4 download module
- APScheduler `AsyncIOScheduler` with an async lock to prevent overlapping jobs
- Local task registry for development and Redis-backed Celery task progress for production
- Local storage backend plus production `S3Storage` upload and presigned URL support
- Login/register/logout with an HTTP-only cookie JWT session

## Run locally

```bash
cd YoutubeArchive
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://127.0.0.1:8000.

On first use, register an account. The first password-backed account is marked as superuser.
If you already have the old local `local@example.com` user in SQLite, registering that email upgrades it with a password and keeps its existing playlists.

## Public test settings

Before exposing the app through a tunnel or temporary domain, set these values in `.env`:

```env
SECRET_KEY=replace-with-a-long-random-secret
APP_BASE_URL=https://your-domain.example
AUTH_COOKIE_SECURE=true
ADMIN_EMAILS=your-email@example.com
REGISTRATION_INVITE_CODE=replace-with-a-private-invite-code
REQUIRE_EMAIL_VERIFICATION=true
```

When `ADMIN_EMAILS` is set, only those emails become superusers. When `REGISTRATION_INVITE_CODE` is set, registration requires the invite code.
Password reset and email verification use SMTP when configured. Without SMTP, email bodies are logged for private testing only.

## Run tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Database migrations

Alembic is wired for production schema management. It reads `DATABASE_URL` from `.env` through the app settings.

For a new production or staging database:

```bash
alembic upgrade head
```

Create future schema revisions after changing SQLAlchemy models:

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

Local development still auto-creates the SQLite schema on startup for convenience. Production startup does not call `create_all()`, so run migrations before starting Uvicorn with `ENVIRONMENT=production`.

## Production task worker

For real users, run sync/download work in Celery instead of the FastAPI process:

```env
ENVIRONMENT=production
TASK_BACKEND=celery
REDIS_URL=redis://localhost:6379/0
REDIS_SOCKET_TIMEOUT_SECONDS=1
REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS=0.5
```

Start the web process and worker as separate services:

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
celery -A worker.celery_app worker --loglevel=info
```

On Windows development machines, Celery may need the solo pool:

```powershell
celery -A worker.celery_app worker --loglevel=info --pool=solo
```

Task status is stored in Redis for `TASK_STATUS_TTL_SECONDS` so `/tasks/active`, `/task/{task_id}/status`, and the global Downloading panel keep working across web processes. Keep Redis socket timeouts short so a Redis hiccup does not make page navigation feel stuck.

## Rate limiting

Basic per-IP rate limits are enabled with SlowAPI for auth, playlist add/sync/delete, settings updates, media streaming, and task status endpoints. This is a guardrail for production, not a full abuse-prevention system; public deployments should still monitor abuse patterns and tune limits over time.

CSRF protection, account lockout, password reset tokens, and email verification tokens are implemented. Set SMTP values in production so verification/reset links are delivered by email.

## Production media storage

Local development stores downloaded media under `downloads/`. For production, configure S3-compatible storage such as Cloudflare R2:

```env
STORAGE_BACKEND=s3
S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
S3_BUCKET_NAME=ytarchive-files
S3_ACCESS_KEY_ID=replace-with-access-key
S3_SECRET_ACCESS_KEY=replace-with-secret-key
S3_PRESIGNED_URL_EXPIRY=3600
```

Downloaded files are uploaded under object keys scoped by user and playlist, for example `users/<owner_id>/playlists/<playlist_id>/<video_id>.mp3`. The `/stream/{video_id}` endpoint returns local files in development and redirects to a presigned URL in S3/R2 mode.

## Deployment files and smoke test

Production helper files live under `deploy/` plus `docker-compose.production.yml`:

- `deploy/env.production.example` - production environment template.
- `deploy/systemd/ytarchive-web.service` - FastAPI service.
- `deploy/systemd/ytarchive-worker.service` - Celery worker service.
- `deploy/nginx/ytarchive.conf` - Nginx HTTPS reverse proxy template.
- `docker-compose.production.yml` - local Postgres + Redis services for a VPS/smoke environment.

After configuring `.env`, running migrations, and starting Redis/Celery/R2 services, run:

```bash
python scripts/smoke_production.py --s3-write
```

The smoke script checks database connectivity, Redis, Celery worker ping, and S3/R2 presigned URL generation. Use `--skip-celery` or `--skip-s3` while bringing services up incrementally.

## Notes

- The first request creates `ytarchive.db`; user accounts are created through `/register`.
- The initial Alembic migration is a baseline for new databases. Migrating existing local SQLite data into PostgreSQL should be handled as a separate export/import step.
- Downloading MP3 requires FFmpeg installed on your machine because `yt-dlp` uses it for audio extraction.
- Auth is intentionally isolated in `auth.py`. Public multi-user production should still add stricter account administration and operational monitoring around the implemented CSRF, password reset, email verification, and lockout flows.
- Production downloads can use `S3Storage` in `storage.py`; local development still uses `LocalStorage`.
- Celery wiring is available in `worker.py`; local development uses in-process background tasks unless `TASK_BACKEND=celery` is set.
