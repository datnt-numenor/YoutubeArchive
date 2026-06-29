import pytest
from fastapi import HTTPException

import auth
from models import User


def test_password_hash_verification() -> None:
    stored_hash = auth.hash_password("secret-password")

    assert stored_hash != "secret-password"
    assert auth.verify_password("secret-password", stored_hash)
    assert not auth.verify_password("wrong-password", stored_hash)
    assert not auth.verify_password("secret-password", auth.LOCAL_DEV_PASSWORD_MARKER)


def test_access_token_round_trip() -> None:
    user = User(id="user-1", email="owner@example.com")

    token = auth.create_access_token(user)
    payload = auth.decode_access_token(token)
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")

    assert payload is not None
    assert payload["sub"] == "user-1"
    assert payload["email"] == "owner@example.com"
    assert auth.decode_access_token(tampered) is None


async def test_register_user_creates_first_superuser_and_authenticates(session) -> None:
    user = await auth.register_user(session, "Owner@Example.COM", "password123")

    assert user.email == "owner@example.com"
    assert user.is_superuser
    assert user.hashed_password != "password123"

    authenticated = await auth.authenticate_user(session, "owner@example.com", "password123")
    rejected = await auth.authenticate_user(session, "owner@example.com", "wrong-password")

    assert authenticated is not None
    assert authenticated.id == user.id
    assert rejected is None

    with pytest.raises(HTTPException):
        await auth.register_user(session, "owner@example.com", "password123")


async def test_register_user_upgrades_local_dev_user(session) -> None:
    local_user = User(
        email="local@example.com",
        hashed_password=auth.LOCAL_DEV_PASSWORD_MARKER,
        is_active=True,
        is_verified=True,
        is_superuser=True,
    )
    session.add(local_user)
    await session.commit()
    await session.refresh(local_user)

    upgraded = await auth.register_user(session, "local@example.com", "password123")
    authenticated = await auth.authenticate_user(session, "local@example.com", "password123")

    assert upgraded.id == local_user.id
    assert upgraded.hashed_password != auth.LOCAL_DEV_PASSWORD_MARKER
    assert upgraded.is_superuser
    assert authenticated is not None
    assert authenticated.id == local_user.id


async def test_register_user_requires_invite_code_when_configured(session, monkeypatch) -> None:
    monkeypatch.setattr(auth.settings, "registration_invite_code", "invite-123")

    with pytest.raises(HTTPException):
        await auth.register_user(session, "owner@example.com", "password123", "wrong-code")

    user = await auth.register_user(session, "owner@example.com", "password123", "invite-123")

    assert user.email == "owner@example.com"


async def test_admin_emails_control_superuser_assignment(session, monkeypatch) -> None:
    monkeypatch.setattr(auth.settings, "admin_emails", "admin@example.com")

    normal_user = await auth.register_user(session, "normal@example.com", "password123")
    admin_user = await auth.register_user(session, "admin@example.com", "password123")

    assert not normal_user.is_superuser
    assert admin_user.is_superuser
