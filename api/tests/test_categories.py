"""§9 step 3: Categories CRUD behind the admin bearer token, and the seed script."""
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Category, CategoryTranslation
from scripts.seed import SEED_CATEGORIES, seed

MATH = {
    "slug": "math",
    "icon": "🔢",
    "translations": {
        "en": {"name": "Math", "description": "Numbers"},
        "es": {"name": "Matemáticas", "description": "Números"},
    },
}


# ---------- auth ----------


@pytest.mark.asyncio
async def test_missing_token_is_rejected(client: AsyncClient):
    resp = await client.get("/admin/categories")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_wrong_token_is_rejected(client: AsyncClient):
    resp = await client.get(
        "/admin/categories", headers={"Authorization": "Bearer definitely-not-it"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_non_bearer_scheme_is_rejected(client: AsyncClient):
    resp = await client.get("/admin/categories", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unconfigured_token_fails_closed(
    client: AsyncClient, admin_headers, monkeypatch: pytest.MonkeyPatch
):
    """An empty ADMIN_TOKEN must not turn into 'any token works'."""
    monkeypatch.setattr(settings, "admin_token", "")
    resp = await client.get("/admin/categories", headers=admin_headers)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_correct_token_is_accepted(client: AsyncClient, admin_headers):
    resp = await client.get("/admin/categories", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json() == []


# ---------- create ----------


@pytest.mark.asyncio
async def test_create_writes_category_and_both_translations(
    client: AsyncClient, admin_headers, db: AsyncSession
):
    resp = await client.post("/admin/categories", json=MATH, headers=admin_headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slug"] == "math"
    assert body["icon"] == "🔢"
    assert body["is_active"] is True
    assert body["sort_order"] == 0
    assert {t["locale"]: t["name"] for t in body["translations"]} == {
        "en": "Math",
        "es": "Matemáticas",
    }

    rows = await db.execute(
        select(CategoryTranslation.locale, CategoryTranslation.name).order_by(
            CategoryTranslation.locale
        )
    )
    assert list(rows) == [("en", "Math"), ("es", "Matemáticas")]


@pytest.mark.asyncio
async def test_duplicate_slug_returns_409(client: AsyncClient, admin_headers, db: AsyncSession):
    first = await client.post("/admin/categories", json=MATH, headers=admin_headers)
    assert first.status_code == 201
    second = await client.post("/admin/categories", json=MATH, headers=admin_headers)
    assert second.status_code == 409
    assert "math" in second.json()["detail"]

    # The rejected request left nothing behind and the session is still usable.
    assert (await db.execute(select(func.count()).select_from(Category))).scalar_one() == 1
    assert (
        await db.execute(select(func.count()).select_from(CategoryTranslation))
    ).scalar_one() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("present", ["en", "es"])
async def test_missing_locale_is_rejected(client: AsyncClient, admin_headers, present: str):
    payload = {**MATH, "translations": {present: MATH["translations"][present]}}
    resp = await client.post("/admin/categories", json=payload, headers=admin_headers)
    assert resp.status_code == 422
    missing = "es" if present == "en" else "en"
    assert missing in resp.text


@pytest.mark.asyncio
async def test_unknown_locale_is_rejected(client: AsyncClient, admin_headers):
    payload = {**MATH, "translations": {**MATH["translations"], "fr": {"name": "Maths"}}}
    resp = await client.post("/admin/categories", json=payload, headers=admin_headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["Math", "world history", "-math", "math-", "mätH"])
async def test_bad_slug_is_rejected(client: AsyncClient, admin_headers, slug: str):
    resp = await client.post("/admin/categories", json={**MATH, "slug": slug}, headers=admin_headers)
    assert resp.status_code == 422


# ---------- list / patch ----------


@pytest.mark.asyncio
async def test_list_orders_by_sort_order_then_slug(client: AsyncClient, admin_headers):
    for slug, order in [("zeta", 1), ("alpha", 1), ("omega", 0)]:
        resp = await client.post(
            "/admin/categories",
            json={**MATH, "slug": slug, "sort_order": order},
            headers=admin_headers,
        )
        assert resp.status_code == 201
    resp = await client.get("/admin/categories", headers=admin_headers)
    assert [c["slug"] for c in resp.json()] == ["omega", "alpha", "zeta"]


@pytest.mark.asyncio
async def test_patch_updates_only_sent_fields(client: AsyncClient, admin_headers):
    created = (await client.post("/admin/categories", json=MATH, headers=admin_headers)).json()

    resp = await client.patch(
        f"/admin/categories/{created['id']}",
        json={"is_active": False, "translations": {"es": {"name": "Mates"}}},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_active"] is False
    assert body["icon"] == "🔢"  # untouched
    by_locale = {t["locale"]: t for t in body["translations"]}
    assert by_locale["en"] == {"locale": "en", "name": "Math", "description": "Numbers"}
    assert by_locale["es"] == {"locale": "es", "name": "Mates", "description": None}


@pytest.mark.asyncio
async def test_patch_unknown_id_is_404(client: AsyncClient, admin_headers):
    resp = await client.patch(
        "/admin/categories/00000000-0000-0000-0000-000000000000",
        json={"is_active": False},
        headers=admin_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_cannot_change_slug(client: AsyncClient, admin_headers):
    created = (await client.post("/admin/categories", json=MATH, headers=admin_headers)).json()
    resp = await client.patch(
        f"/admin/categories/{created['id']}", json={"slug": "maths"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["slug"] == "math"


# ---------- seed ----------


async def _category_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(Category))).scalar_one()


@pytest.mark.asyncio
async def test_seed_twice_leaves_six_categories(db: AsyncSession):
    assert len(SEED_CATEGORIES) == 6
    assert {c.slug for c in SEED_CATEGORIES} == {
        "math", "bible", "world-history", "music", "cars", "science",
    }

    first = await seed(db)
    assert sorted(first) == sorted(c.slug for c in SEED_CATEGORIES)
    assert await _category_count(db) == 6

    second = await seed(db)
    assert second == []
    assert await _category_count(db) == 6
    assert (
        await db.execute(select(func.count()).select_from(CategoryTranslation))
    ).scalar_one() == 12


@pytest.mark.asyncio
async def test_seed_does_not_overwrite_edited_names(db: AsyncSession):
    await seed(db)
    row = (
        await db.execute(
            select(CategoryTranslation)
            .join(Category)
            .where(Category.slug == "math", CategoryTranslation.locale == "en")
        )
    ).scalar_one()
    row.name = "Mathematics"
    await db.flush()

    await seed(db)
    await db.refresh(row)
    assert row.name == "Mathematics"


@pytest.mark.asyncio
async def test_seed_fills_in_a_deleted_category(client: AsyncClient, admin_headers, db: AsyncSession):
    """Seed creates only what's missing; the surviving five are untouched."""
    await seed(db)
    cars = (await db.execute(select(Category).where(Category.slug == "cars"))).scalar_one()
    await db.delete(cars)
    await db.flush()
    assert await _category_count(db) == 5

    assert await seed(db) == ["cars"]
    resp = await client.get("/admin/categories", headers=admin_headers)
    assert [c["slug"] for c in resp.json()] == [
        "math", "bible", "world-history", "music", "cars", "science",
    ]
