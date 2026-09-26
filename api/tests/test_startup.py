"""Startup check for ADMIN_USER_ID (app.auth.verify_admin_user, wired into
the app lifespan)."""
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app import db as app_db
from app.auth import verify_admin_user
from app.config import settings
from app import main
from app.main import app, lifespan
from app.models import User


async def _user(db: AsyncSession, role: str) -> User:
    user = User(display_name=role, role=role)
    db.add(user)
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_unset_is_a_no_op(db: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_user_id", None)
    await verify_admin_user(db)


@pytest.mark.asyncio
async def test_admin_user_passes(db: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    admin = await _user(db, "admin")
    monkeypatch.setattr(settings, "admin_user_id", admin.id)
    await verify_admin_user(db)


@pytest.mark.asyncio
async def test_unknown_id_fails(db: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    missing = uuid.uuid4()
    monkeypatch.setattr(settings, "admin_user_id", missing)
    with pytest.raises(RuntimeError, match=f"{missing} is not a users.id"):
        await verify_admin_user(db)


@pytest.mark.asyncio
async def test_non_admin_role_fails(db: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    player = await _user(db, "player")
    monkeypatch.setattr(settings, "admin_user_id", player.id)
    with pytest.raises(RuntimeError, match="has role 'player', expected 'admin'"):
        await verify_admin_user(db)


@pytest.fixture
def lifespan_session(db: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    """Point the lifespan's SessionLocal at the test's rolled-back session
    (without closing it on exit, the way a real session would be)."""

    @asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(app_db, "SessionLocal", _session)


@pytest.mark.asyncio
async def test_lifespan_refuses_to_boot_on_bad_admin_user(
    db: AsyncSession, lifespan_session, monkeypatch: pytest.MonkeyPatch
):
    player = await _user(db, "player")
    monkeypatch.setattr(settings, "admin_user_id", player.id)
    with pytest.raises(RuntimeError, match="expected 'admin'"):
        async with lifespan(app):
            pass


@pytest.mark.asyncio
async def test_lifespan_boots_with_admin_user(
    db: AsyncSession, lifespan_session, monkeypatch: pytest.MonkeyPatch
):
    admin = await _user(db, "admin")
    monkeypatch.setattr(settings, "admin_user_id", admin.id)
    async with lifespan(app):
        pass


@pytest.mark.asyncio
async def test_lifespan_skips_the_check_when_unset(
    db: AsyncSession, lifespan_session, monkeypatch: pytest.MonkeyPatch
):
    """No ADMIN_USER_ID → the check is not run (the resume pass still
    opens a session to look for running games)."""
    monkeypatch.setattr(settings, "admin_user_id", None)

    async def _never(session):
        raise AssertionError("verify_admin_user must not be called")

    monkeypatch.setattr(main, "verify_admin_user", _never)
    async with lifespan(app):
        pass
