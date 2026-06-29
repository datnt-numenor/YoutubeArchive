# YouTube Playlist Archiver

FastAPI app for archiving YouTube playlist metadata and downloading playlist media locally with `yt-dlp`.

## What is included

- FastAPI + Jinja2 UI
- Async SQLAlchemy + SQLite default database
- Pydantic v2 schemas
- `yt-dlp` metadata extraction and MP3/MP4 download module
- APScheduler `AsyncIOScheduler` with an async lock to prevent overlapping jobs
- Local task registry for sync progress polling
- Local storage backend with a production `S3Storage` seam ready for implementation
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
AUTH_COOKIE_SECURE=true
ADMIN_EMAILS=your-email@example.com
REGISTRATION_INVITE_CODE=replace-with-a-private-invite-code
```

When `ADMIN_EMAILS` is set, only those emails become superusers. When `REGISTRATION_INVITE_CODE` is set, registration requires the invite code.

## Run tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Notes

- The first request creates `ytarchive.db`; user accounts are created through `/register`.
- Downloading MP3 requires FFmpeg installed on your machine because `yt-dlp` uses it for audio extraction.
- Auth is intentionally isolated in `auth.py`. The current cookie JWT flow is suitable for a private/local deployment; public multi-user production should add CSRF protection, password reset, email verification, and stricter account administration.
- Production downloads should move from `LocalStorage` to the `S3Storage` implementation in `storage.py`.
- Celery wiring is available in `worker.py`; the local web app currently uses in-process background tasks for easier development.
