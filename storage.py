from pathlib import Path

from fastapi import HTTPException, status

from config import settings


class StorageBackend:
    async def upload_file(self, local_path: Path, object_key: str) -> str:
        raise NotImplementedError

    async def get_file_url(self, object_key: str) -> str:
        raise NotImplementedError


class LocalStorage(StorageBackend):
    async def upload_file(self, local_path: Path, object_key: str) -> str:
        return str(local_path.relative_to(settings.downloads_dir.parent))

    async def get_file_url(self, object_key: str) -> str:
        path = settings.downloads_dir.parent / object_key
        if not path.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found")
        return str(path)


class S3Storage(StorageBackend):
    async def upload_file(self, local_path: Path, object_key: str) -> str:
        raise NotImplementedError("S3/R2 upload is configured here for production implementation")

    async def get_file_url(self, object_key: str) -> str:
        raise NotImplementedError("S3/R2 presigned URLs are configured here for production implementation")


def get_storage_backend() -> StorageBackend:
    if settings.environment == "production" and settings.s3_bucket_name:
        return S3Storage()
    return LocalStorage()


storage = get_storage_backend()
