import asyncio
import random
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import yt_dlp

from config import settings


DownloadFormat = Literal["mp3", "mp4"]


class DownloaderError(RuntimeError):
    pass


class YtDlpLogCollector:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def message(self) -> str:
        if self.errors:
            return self.errors[-1]
        if self.warnings:
            return self.warnings[-1]
        return "yt-dlp failed"


def canonicalize_playlist_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    playlist_ids = query.get("list")
    if not playlist_ids:
        return url

    playlist_id = playlist_ids[0]
    if parsed.netloc.endswith("youtube.com") or parsed.netloc.endswith("youtu.be"):
        return urlunparse(("https", "www.youtube.com", "/playlist", "", urlencode({"list": playlist_id}), ""))
    return url


def extract_playlist_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    playlist_ids = parse_qs(parsed.query).get("list")
    if not playlist_ids or not playlist_ids[0].strip():
        raise DownloaderError("Use a public YouTube playlist URL that includes a list= playlist id.")
    return playlist_ids[0].strip()


def _best_thumbnail(entry: dict[str, Any]) -> str | None:
    thumbnails = entry.get("thumbnails") or []
    if thumbnails:
        return thumbnails[-1].get("url")
    return entry.get("thumbnail")


def _normalize_video(entry: dict[str, Any]) -> dict[str, Any] | None:
    video_id = entry.get("id") or entry.get("url")
    if not video_id:
        return None
    return {
        "yt_video_id": str(video_id),
        "title": entry.get("title") or f"YouTube video {video_id}",
        "channel_name": entry.get("channel") or entry.get("uploader") or "",
        "duration": int(entry.get("duration") or 0),
        "thumbnail_url": _best_thumbnail(entry),
    }


def _extract_playlist_sync(url: str) -> dict[str, Any]:
    playlist_url = canonicalize_playlist_url(url)
    options = {
        "extract_flat": True,
        "ignoreerrors": True,
        "quiet": True,
        "skip_download": True,
        "noplaylist": False,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(playlist_url, download=False)

    if not info:
        raise DownloaderError("yt-dlp did not return playlist metadata")

    entries = [_normalize_video(entry) for entry in info.get("entries", []) if entry]
    videos = [entry for entry in entries if entry]
    if not videos:
        raise DownloaderError("No videos found. Use a public YouTube playlist URL, not a private or unavailable playlist.")

    return {
        "yt_playlist_id": str(info.get("id") or url),
        "title": info.get("title") or "Untitled playlist",
        "url": info.get("webpage_url") or playlist_url,
        "videos": videos,
    }


async def extract_playlist_metadata(url: str) -> dict[str, Any]:
    return await asyncio.to_thread(_extract_playlist_sync, url)


def _find_downloaded_file(video_id: str, output_dir: Path, format_: DownloadFormat) -> Path | None:
    preferred_ext = ".mp3" if format_ == "mp3" else ".mp4"
    preferred = output_dir / f"{video_id}{preferred_ext}"
    if preferred.exists():
        return preferred

    candidates = [
        path
        for path in output_dir.glob(f"{video_id}.*")
        if path.is_file() and not path.name.endswith(".part") and path.suffix.lower() == preferred_ext
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[0]


def _cleanup_temporary_downloads(video_id: str, output_dir: Path, keep_path: Path | None = None) -> None:
    keep_resolved = keep_path.resolve() if keep_path else None
    temporary_suffixes = {".part", ".ytdl", ".temp", ".tmp"}

    for path in output_dir.glob(f"{video_id}.*"):
        if not path.is_file():
            continue
        if keep_resolved and path.resolve() == keep_resolved:
            continue

        should_delete = path.name.endswith(".part") or path.suffix.lower() in temporary_suffixes
        if keep_path and keep_path.suffix.lower() == ".mp3":
            should_delete = should_delete or path.suffix.lower() in {".webm", ".m4a", ".opus"}

        if should_delete:
            path.unlink(missing_ok=True)


def _download_video_sync(video_id: str, output_dir: Path, format_: DownloadFormat) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_download = _find_downloaded_file(video_id, output_dir, format_)
    if existing_download:
        _cleanup_temporary_downloads(video_id, output_dir, existing_download)
        return existing_download

    outtmpl = str(output_dir / "%(id)s.%(ext)s")
    url = f"https://www.youtube.com/watch?v={video_id}"
    log_collector = YtDlpLogCollector()

    options: dict[str, Any] = {
        "ignoreerrors": True,
        "quiet": False,
        "no_warnings": False,
        "logger": log_collector,
        "outtmpl": outtmpl,
        "restrictfilenames": True,
        "sleep_interval": random.randint(settings.yt_sleep_min_seconds, settings.yt_sleep_max_seconds),
        "max_sleep_interval": settings.yt_sleep_max_seconds,
    }
    if format_ == "mp3":
        options.update(
            {
                "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )
    else:
        options.update({"format": "bestvideo+bestaudio/best", "merge_output_format": "mp4"})

    before = set(output_dir.glob(f"{video_id}.*"))
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            result = ydl.download([url])

        downloaded_file = _find_downloaded_file(video_id, output_dir, format_)
        if downloaded_file:
            _cleanup_temporary_downloads(video_id, output_dir, downloaded_file)
            return downloaded_file

        if result not in (0, None):
            raise DownloaderError(log_collector.message())

        after = set(output_dir.glob(f"{video_id}.*"))
        created = sorted(
            [
                path
                for path in after - before
                if path.is_file() and not path.name.endswith(".part")
            ],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if created:
            _cleanup_temporary_downloads(video_id, output_dir, created[0])
            return created[0]

        raise DownloaderError(f"No downloaded file found for video {video_id}")
    except Exception:
        _cleanup_temporary_downloads(video_id, output_dir)
        raise


async def download_video(video_id: str, output_dir: Path, format_: DownloadFormat) -> Path:
    return await asyncio.to_thread(_download_video_sync, video_id, output_dir, format_)
