import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_session
from models import User

LOCAL_DEV_PASSWORD_MARKER = "local-dev-user"
PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 260_000


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return "$".join(
        [
            PASSWORD_ALGORITHM,
            str(PASSWORD_ITERATIONS),
            _base64url_encode(salt),
            _base64url_encode(password_hash),
        ]
    )


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash or stored_hash == LOCAL_DEV_PASSWORD_MARKER:
        return False

    try:
        algorithm, iterations_value, salt_value, expected_value = stored_hash.split("$", 3)
        iterations = int(iterations_value)
    except ValueError:
        return False

    if algorithm != PASSWORD_ALGORITHM:
        return False

    salt = _base64url_decode(salt_value)
    expected = _base64url_decode(expected_value)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(password_hash, expected)


def _jwt_signature(signing_input: str) -> str:
    digest = hmac.new(settings.secret_key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return _base64url_encode(digest)


def create_access_token(user: User) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user.id,
        "email": user.email,
        "iat": now,
        "exp": now + settings.auth_cookie_max_age_seconds,
    }
    encoded_header = _base64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_payload = _base64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_payload}"
    return f"{signing_input}.{_jwt_signature(signing_input)}"


def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        encoded_header, encoded_payload, signature = token.split(".", 2)
    except ValueError:
        return None

    signing_input = f"{encoded_header}.{encoded_payload}"
    if not hmac.compare_digest(_jwt_signature(signing_input), signature):
        return None

    try:
        payload = json.loads(_base64url_decode(encoded_payload))
    except (json.JSONDecodeError, ValueError):
        return None

    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def set_login_cookie(response: Response, user: User) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=create_access_token(user),
        max_age=settings.auth_cookie_max_age_seconds,
        httponly=True,
        secure=settings.auth_cookie_secure_enabled,
        samesite="lax",
    )


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        httponly=True,
        secure=settings.auth_cookie_secure_enabled,
        samesite="lax",
    )


async def count_password_users(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count(User.id)).where(User.hashed_password.notin_([LOCAL_DEV_PASSWORD_MARKER, ""]))
    )
    return int(result.scalar_one())


def is_admin_email(email: str, password_user_count: int) -> bool:
    admin_emails = settings.admin_email_set
    if admin_emails:
        return normalize_email(email) in admin_emails
    return password_user_count == 0


def validate_registration_invite(invite_code: str | None) -> None:
    expected = settings.registration_invite_code
    if expected and not hmac.compare_digest(invite_code or "", expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid registration invite code")


async def register_user(
    session: AsyncSession,
    email: str,
    password: str,
    invite_code: str | None = None,
) -> User:
    validate_registration_invite(invite_code)
    normalized_email = normalize_email(email)
    existing_result = await session.execute(select(User).where(User.email == normalized_email))
    existing_user = existing_result.scalar_one_or_none()
    password_user_count = await count_password_users(session)

    if existing_user:
        if existing_user.hashed_password != LOCAL_DEV_PASSWORD_MARKER:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email is already registered")

        existing_user.hashed_password = hash_password(password)
        existing_user.is_active = True
        existing_user.is_verified = True
        existing_user.is_superuser = existing_user.is_superuser or is_admin_email(normalized_email, password_user_count)
        await session.commit()
        await session.refresh(existing_user)
        return existing_user

    user = User(
        email=normalized_email,
        hashed_password=hash_password(password),
        is_active=True,
        is_verified=True,
        is_superuser=is_admin_email(normalized_email, password_user_count),
        playlist_quota=settings.default_playlist_quota,
        storage_quota_bytes=settings.storage_quota_bytes,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def authenticate_user(session: AsyncSession, email: str, password: str) -> User | None:
    result = await session.execute(select(User).where(User.email == normalize_email(email)))
    user = result.scalar_one_or_none()
    if not user or not user.is_active or not verify_password(password, user.hashed_password):
        return None
    return user


async def get_optional_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User | None:
    token = request.cookies.get(settings.auth_cookie_name)
    if not token:
        return None

    payload = decode_access_token(token)
    if not payload:
        return None

    result = await session.execute(select(User).where(User.id == payload.get("sub")))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        return None
    return user


async def get_current_user(user: User | None = Depends(get_optional_user)) -> User:
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user


async def require_superuser(user: User = Depends(get_current_user)) -> User:
    if not user.is_superuser:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Superuser access required")
    return user
