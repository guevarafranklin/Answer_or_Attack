"""§9 step 7: the four health endpoints (spec §4) read question_stats only,
with the serves >= 50 floor."""
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Category,
    Question,
    QuestionServe,
    QuestionStats,
    QuestionTranslation,
    StudyPack,
    User,
)
from app.services.health import HEALTH_MIN_SERVES
from app.services.validation import content_hash

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
EMPTY = {"pending": 0, "live": 0, "archived": 0, "rejected": 0}


async def _category(db: AsyncSession, slug: str) -> Category:
    cat = Category(slug=slug)
    db.add(cat)
    await db.flush()
    return cat


async def _question(
    db: AsyncSession,
    category: Category,
    *,
    status: str = "live",
    locales: tuple[str, ...] = ("en", "es"),
    **fields,
) -> Question:
    i = uuid.uuid4().hex[:6]
    q = Question(
        category_id=category.id,
        difficulty=2,
        correct_index=0,
        status=status,
        content_hash=content_hash(f"Q {i}"),
        translations=[
            QuestionTranslation(locale=loc, stem=f"Q {i} {loc}?", options=["a", "b", "c", "d"])
            for loc in locales
        ],
        **fields,
    )
    db.add(q)
    await db.flush()
    return q


async def _stats(
    db: AsyncSession,
    question: Question,
    *,
    serves: int,
    correct: int = 0,
    incorrect: int = 0,
    timeouts: int = 0,
    reports: int = 0,
) -> QuestionStats:
    s = QuestionStats(
        question_id=question.id,
        serves=serves,
        correct=correct,
        incorrect=incorrect,
        timeouts=timeouts,
        reports=reports,
        avg_response_ms=4000,
        last_served_at=T0,
    )
    db.add(s)
    await db.flush()
    return s


def _ids(resp) -> list[str]:
    return [row["question_id"] for row in resp.json()["items"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/easy", "/suspect", "/dead", "/summary"])
async def test_requires_admin(client: AsyncClient, path: str):
    resp = await client.get(f"/admin/health{path}")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_unknown_view_is_422(client: AsyncClient, admin_headers):
    resp = await client.get("/admin/health/broken", headers=admin_headers)
    assert resp.status_code == 422  # not one of easy|suspect|dead


@pytest.mark.asyncio
async def test_easy_ranks_by_correct_ratio_above_the_floor(
    client: AsyncClient, admin_headers, db: AsyncSession
):
    cat = await _category(db, "math")
    q95 = await _question(db, cat)
    q100 = await _question(db, cat)
    q90 = await _question(db, cat)
    tiny = await _question(db, cat)
    await _stats(db, q95, serves=100, correct=95, incorrect=5)
    await _stats(db, q100, serves=HEALTH_MIN_SERVES, correct=HEALTH_MIN_SERVES)
    await _stats(db, q90, serves=100, correct=90, incorrect=10)  # not > 0.90
    await _stats(db, tiny, serves=HEALTH_MIN_SERVES - 1, correct=HEALTH_MIN_SERVES - 1)

    resp = await client.get("/admin/health/easy", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert _ids(resp) == [str(q100.id), str(q95.id)]
    assert (body["view"], body["total"], body["page"], body["page_size"]) == ("easy", 2, 1, 50)
    top = body["items"][0]
    assert top["ratio"] == 1.0
    assert (top["serves"], top["correct"], top["absents"]) == (50, 50, 0)
    assert top["avg_response_ms"] == 4000
    assert top["question"]["id"] == str(q100.id)
    assert {t["locale"] for t in top["question"]["translations"]} == {"en", "es"}
    assert top["question"]["status"] == "live"


@pytest.mark.asyncio
async def test_suspect_is_low_ratio_or_reports_worst_first(
    client: AsyncClient, admin_headers, db: AsyncSession
):
    cat = await _category(db, "math")
    wrong_key = await _question(db, cat)
    reported = await _question(db, cat)
    fine = await _question(db, cat)
    three_reports = await _question(db, cat)
    unproven = await _question(db, cat)
    await _stats(db, wrong_key, serves=80, correct=10, incorrect=70)  # 0.125
    await _stats(db, reported, serves=60, correct=40, incorrect=20, reports=4)  # > 3 reports
    await _stats(db, fine, serves=60, correct=15, incorrect=45, reports=3)  # 0.25 exactly, 3
    await _stats(db, three_reports, serves=60, correct=50, reports=3)
    await _stats(db, unproven, serves=10, correct=0, incorrect=10, reports=9)  # under the floor

    resp = await client.get("/admin/health/suspect", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert _ids(resp) == [str(wrong_key.id), str(reported.id)]
    assert [round(r["ratio"], 3) for r in resp.json()["items"]] == [0.125, 0.667]


@pytest.mark.asyncio
async def test_dead_is_timeout_ratio(client: AsyncClient, admin_headers, db: AsyncSession):
    cat = await _category(db, "math")
    dead = await _question(db, cat)
    deader = await _question(db, cat)
    slow = await _question(db, cat)
    await _stats(db, dead, serves=100, correct=10, timeouts=65)
    await _stats(db, deader, serves=100, correct=0, timeouts=90)
    await _stats(db, slow, serves=100, correct=40, timeouts=60)  # not > 0.60

    resp = await client.get("/admin/health/dead", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert _ids(resp) == [str(deader.id), str(dead.id)]
    assert [r["ratio"] for r in resp.json()["items"]] == [0.9, 0.65]


@pytest.mark.asyncio
async def test_views_paginate(client: AsyncClient, admin_headers, db: AsyncSession):
    cat = await _category(db, "math")
    for _ in range(5):
        await _stats(db, await _question(db, cat), serves=100, timeouts=100)

    page1 = await client.get("/admin/health/dead?page_size=2", headers=admin_headers)
    page3 = await client.get("/admin/health/dead?page_size=2&page=3", headers=admin_headers)
    assert (len(_ids(page1)), page1.json()["total"]) == (2, 5)
    assert (len(_ids(page3)), page3.json()["page"]) == (1, 3)
    assert not set(_ids(page1)) & set(_ids(page3))
    assert (await client.get("/admin/health/dead?page=0", headers=admin_headers)).status_code == 422
    assert (
        await client.get("/admin/health/dead?page_size=201", headers=admin_headers)
    ).status_code == 422


@pytest.mark.asyncio
async def test_views_read_question_stats_not_serves(
    client: AsyncClient, admin_headers, db: AsyncSession
):
    """Raw serves that have not been rolled up are invisible to every view."""
    cat = await _category(db, "math")
    q = await _question(db, cat)
    db.add_all(
        QuestionServe(question_id=q.id, session_id=uuid.uuid4(), locale="en", outcome="timeout")
        for _ in range(100)
    )
    await db.flush()
    for view in ("easy", "suspect", "dead"):
        resp = await client.get(f"/admin/health/{view}", headers=admin_headers)
        assert resp.json()["items"] == [], view
    summary = (await client.get("/admin/health/summary", headers=admin_headers)).json()
    assert summary["health"] == {"easy": 0, "suspect": 0, "dead": 0}


@pytest.mark.asyncio
async def test_summary_counts(client: AsyncClient, admin_headers, db: AsyncSession):
    math, bible = await _category(db, "math"), await _category(db, "bible")
    await _question(db, math, status="pending")
    await _question(db, math, status="pending", locales=("en",))
    await _question(db, math, status="live")
    await _question(db, bible, status="live")
    await _question(db, bible, status="rejected")
    await _question(db, bible, status="archived", locales=("es",))
    # Pack content is not house content and stays out of the dashboard.
    owner = User(display_name="owner")
    db.add(owner)
    await db.flush()
    pack = StudyPack(owner_id=owner.id, title="p", locale="en")
    db.add(pack)
    await db.flush()
    await _question(db, math, status="pending", pack_id=pack.id)
    # Health counts come from question_stats.
    easy = await _question(db, math)
    dead = await _question(db, bible)
    await _stats(db, easy, serves=100, correct=99, reports=5)  # easy and suspect (reports)
    await _stats(db, dead, serves=100, correct=20, timeouts=80)  # dead and suspect (ratio)

    resp = await client.get("/admin/health/summary", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pending_backlog"] == 2
    assert body["by_status"] == {"pending": 2, "live": 4, "archived": 1, "rejected": 1}
    assert body["by_category"] == [
        {"slug": "bible", "counts": {**EMPTY, "live": 2, "rejected": 1, "archived": 1}},
        {"slug": "math", "counts": {**EMPTY, "pending": 2, "live": 2}},
    ]
    assert body["by_locale"] == {
        "en": {"pending": 2, "live": 4, "archived": 0, "rejected": 1},
        "es": {"pending": 1, "live": 4, "archived": 1, "rejected": 1},
    }
    assert body["health"] == {"easy": 1, "suspect": 2, "dead": 1}


@pytest.mark.asyncio
async def test_summary_on_an_empty_bank(client: AsyncClient, admin_headers, db: AsyncSession):
    body = (await client.get("/admin/health/summary", headers=admin_headers)).json()
    assert body == {
        "pending_backlog": 0,
        "by_status": EMPTY,
        "by_category": [],
        "by_locale": {"en": EMPTY, "es": EMPTY},
        "health": {"easy": 0, "suspect": 0, "dead": 0},
    }
    assert await db.scalar(select(Question.id)) is None
