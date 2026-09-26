"""The X-User-Id player stub (app.auth.current_player) exists only in dev:
ENV defaults to prod, where every player route is a 401 no matter what
the header says."""
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import PLAYER_HEADER
from app.cache import get_redis
from app.config import Settings, settings
from app.main import app
from app.models import GameSession, QuestionReport, User

PLAYER_ROUTES = [
    ("/questions/{}/report", {"reason": "typo"}),
    ("/sessions", {"locale": "en"}),
]


def test_env_defaults_to_prod_and_is_closed():
    cfg = Settings(_env_file=None, database_url="postgresql+psycopg://x/y")
    assert cfg.env == "prod"
    assert Settings(_env_file=None, database_url="postgresql+psycopg://x/y", env="dev").env == "dev"
    with pytest.raises(ValueError):
        Settings(_env_file=None, database_url="postgresql+psycopg://x/y", env="staging")


class _NoRedis:
    async def set(self, *a, **kw):
        raise AssertionError("must not be reached")


@pytest.fixture
def no_redis():
    app.dependency_overrides[get_redis] = _NoRedis
    yield
    app.dependency_overrides.pop(get_redis, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", PLAYER_ROUTES)
async def test_player_stub_is_401_outside_dev(
    client: AsyncClient,
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    no_redis,
    path: str,
    body,
):
    """A real user with a well-formed header: fine in dev (the request gets
    past auth), refused in prod before anything is looked at."""
    user = User(display_name="p")
    db.add(user)
    await db.flush()
    headers = {PLAYER_HEADER: str(user.id)}
    url = path.format(uuid.uuid4())

    assert settings.env == "dev"  # the client fixture's default
    in_dev = await client.post(url, json=body, headers=headers)
    assert in_dev.status_code != 401, in_dev.text
    sessions_before = await db.scalar(select(func.count()).select_from(GameSession))

    monkeypatch.setattr(settings, "env", "prod")
    in_prod = await client.post(url, json=body, headers=headers)
    assert in_prod.status_code == 401
    assert in_prod.json()["detail"] == "player auth is not available"
    # Nothing player-side was written by the refused call.
    assert await db.scalar(select(func.count()).select_from(QuestionReport)) == 0
    assert await db.scalar(select(func.count()).select_from(GameSession)) == sessions_before
