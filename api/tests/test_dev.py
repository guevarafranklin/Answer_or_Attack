"""Phase 2 §10 step 9: the dev-only routes behind the web test client
(spec §7): the page, guest users, the category list — and that none of
them exist outside ENV=dev."""
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import PLAYER_HEADER
from app.config import settings
from app.models import Category, CategoryTranslation, User
from app.routers.dev import CLIENT_HTML


@pytest.mark.asyncio
async def test_client_page_is_served_in_dev(client: AsyncClient):
    r = await client.get("/dev/client")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>Answer or Attack" in r.text
    assert CLIENT_HTML.is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, path, body",
    [
        ("GET", "/dev/client", None),
        ("POST", "/dev/guest", {"display_name": "x"}),
        ("GET", "/dev/categories", None),
        ("GET", "/dev/sessions/ABCDEF/report", None),
    ],
)
async def test_dev_routes_are_404_outside_dev(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body
):
    monkeypatch.setattr(settings, "env", "prod")
    r = await client.request(method, path, json=body)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_guest_creates_a_user_the_player_stub_accepts(client: AsyncClient, db: AsyncSession):
    r = await client.post("/dev/guest", json={"display_name": "  Ana  "})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["display_name"] == "Ana"
    user = await db.get(User, uuid.UUID(body["user_id"]))
    assert user is not None and user.role == "player" and user.email is None

    # The id works as X-User-Id: the guest can host a lobby.
    r = await client.post(
        "/sessions",
        json={"category_ids": [], "locale": "en"},
        headers={PLAYER_HEADER: body["user_id"]},
    )
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_guest_needs_a_name(client: AsyncClient):
    assert (await client.post("/dev/guest", json={"display_name": ""})).status_code == 422
    assert (await client.post("/dev/guest", json={})).status_code == 422


@pytest.mark.asyncio
async def test_categories_lists_active_ones_with_both_names(client: AsyncClient, db: AsyncSession):
    def cat(slug: str, active: bool) -> Category:
        return Category(
            slug=slug,
            icon="🎲",
            is_active=active,
            translations=[
                CategoryTranslation(locale="en", name=f"{slug} en"),
                CategoryTranslation(locale="es", name=f"{slug} es"),
            ],
        )

    live, dead = cat("dev-live", True), cat("dev-dead", False)
    db.add_all([live, dead])
    await db.flush()

    r = await client.get("/dev/categories")
    assert r.status_code == 200
    by_slug = {c["slug"]: c for c in r.json()}
    assert "dev-dead" not in by_slug
    got = by_slug["dev-live"]
    assert got["id"] == str(live.id)
    assert got["icon"] == "🎲"
    assert got["names"] == {"en": "dev-live en", "es": "dev-live es"}
