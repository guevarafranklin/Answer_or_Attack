"""§9 step 8: POST /questions/{id}/report (spec §4 Player reports)."""
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import PLAYER_HEADER
from app.models import Category, Question, QuestionReport, QuestionStats, QuestionTranslation, User
from app.services.validation import content_hash


async def _question(db: AsyncSession) -> Question:
    cat = Category(slug=f"c-{uuid.uuid4().hex[:6]}")
    db.add(cat)
    await db.flush()
    i = uuid.uuid4().hex[:6]
    q = Question(
        category_id=cat.id,
        difficulty=2,
        correct_index=0,
        status="live",
        content_hash=content_hash(f"Q {i}"),
        translations=[
            QuestionTranslation(locale=loc, stem=f"Q {i} {loc}?", options=["a", "b", "c", "d"])
            for loc in ("en", "es")
        ],
    )
    db.add(q)
    await db.flush()
    return q


async def _player(db: AsyncSession) -> User:
    user = User(display_name=f"player {uuid.uuid4().hex[:4]}")
    db.add(user)
    await db.flush()
    return user


def _as(user: User) -> dict[str, str]:
    return {PLAYER_HEADER: str(user.id)}


async def _stats(db: AsyncSession, q: Question) -> QuestionStats | None:
    return await db.get(QuestionStats, q.id, populate_existing=True)


@pytest.mark.asyncio
async def test_requires_a_known_player(client: AsyncClient, db: AsyncSession):
    q = await _question(db)
    body = {"reason": "typo"}
    assert (await client.post(f"/questions/{q.id}/report", json=body)).status_code == 401
    ghost = {PLAYER_HEADER: str(uuid.uuid4())}
    assert (
        await client.post(f"/questions/{q.id}/report", json=body, headers=ghost)
    ).status_code == 401
    junk = {PLAYER_HEADER: "not-a-uuid"}
    assert (
        await client.post(f"/questions/{q.id}/report", json=body, headers=junk)
    ).status_code == 401
    assert await db.scalar(select(func.count()).select_from(QuestionReport)) == 0


@pytest.mark.asyncio
async def test_report_creates_row_and_stats(client: AsyncClient, db: AsyncSession):
    """First report on a never-served question: the stats row is created
    with reports=1, everything else at zero."""
    q, player = await _question(db), await _player(db)
    session_id = uuid.uuid4()
    resp = await client.post(
        f"/questions/{q.id}/report",
        json={"reason": "wrong_answer", "note": "it's B", "session_id": str(session_id)},
        headers=_as(player),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["question_id"], body["user_id"]) == (str(q.id), str(player.id))
    assert (body["reason"], body["note"]) == ("wrong_answer", "it's B")
    assert body["session_id"] == str(session_id)
    assert body["resolved"] is False

    s = await _stats(db, q)
    assert (s.reports, s.serves, s.correct) == (1, 0, 0)


@pytest.mark.asyncio
async def test_report_increments_existing_stats_only(client: AsyncClient, db: AsyncSession):
    q = await _question(db)
    db.add(QuestionStats(question_id=q.id, serves=80, correct=60, reports=2))
    await db.flush()
    for _ in range(2):
        player = await _player(db)
        resp = await client.post(
            f"/questions/{q.id}/report", json={"reason": "confusing"}, headers=_as(player)
        )
        assert resp.status_code == 201, resp.text
    s = await _stats(db, q)
    assert (s.reports, s.serves, s.correct) == (4, 80, 60)


@pytest.mark.asyncio
async def test_one_report_per_user_per_question(client: AsyncClient, db: AsyncSession):
    q, other, player = await _question(db), await _question(db), await _player(db)
    first = await client.post(
        f"/questions/{q.id}/report", json={"reason": "typo"}, headers=_as(player)
    )
    again = await client.post(
        f"/questions/{q.id}/report", json={"reason": "offensive"}, headers=_as(player)
    )
    assert (first.status_code, again.status_code) == (201, 409)
    # Not counted twice, and the session is still usable afterwards.
    assert (await _stats(db, q)).reports == 1
    assert await db.scalar(select(func.count()).select_from(QuestionReport)) == 1
    # Same user, another question: fine.
    resp = await client.post(
        f"/questions/{other.id}/report", json={"reason": "typo"}, headers=_as(player)
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_report_unknown_question_is_404(client: AsyncClient, db: AsyncSession):
    player = await _player(db)
    resp = await client.post(
        f"/questions/{uuid.uuid4()}/report", json={"reason": "typo"}, headers=_as(player)
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_report_validates_body(client: AsyncClient, db: AsyncSession):
    q, player = await _question(db), await _player(db)
    bad_reason = await client.post(
        f"/questions/{q.id}/report", json={"reason": "meh"}, headers=_as(player)
    )
    long_note = await client.post(
        f"/questions/{q.id}/report",
        json={"reason": "other", "note": "x" * 1001},
        headers=_as(player),
    )
    assert (bad_reason.status_code, long_note.status_code) == (422, 422)
    assert await _stats(db, q) is None
