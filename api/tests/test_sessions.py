"""§9 step 8: POST /sessions/generate (spec §4 Session content)."""
import json
import time
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import PLAYER_HEADER
from app.cache import SESSION_QUESTIONS_TTL, get_redis
from app.main import app
from app.models import (
    Category,
    GameSession,
    Question,
    QuestionTranslation,
    SessionQuestion,
    StudyPack,
    User,
)
from app.services import sessions as svc
from app.services.validation import content_hash


class FakeRedis:
    def __init__(self):
        self.store: dict[str, tuple[str, timedelta | None]] = {}
        self.down = False

    async def set(self, key: str, value: str, ex: timedelta | None = None) -> None:
        if self.down:
            raise ConnectionError("redis is down")
        self.store[key] = (value, ex)


@pytest.fixture
def redis():
    fake = FakeRedis()
    app.dependency_overrides[get_redis] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_redis, None)


@pytest_asyncio.fixture
async def player(db: AsyncSession) -> User:
    user = User(display_name="host")
    db.add(user)
    await db.flush()
    return user


@pytest.fixture
def headers(player: User) -> dict[str, str]:
    return {PLAYER_HEADER: str(player.id)}


@pytest_asyncio.fixture
async def category(db: AsyncSession) -> Category:
    cat = Category(slug=f"c-{uuid.uuid4().hex[:6]}")
    db.add(cat)
    await db.flush()
    return cat


async def _bank(
    db: AsyncSession,
    category: Category,
    n: int,
    *,
    status: str = "live",
    pack_id: uuid.UUID | None = None,
    region: str = "global",
    difficulty: int | None = None,
    locales: tuple[str, ...] = ("en", "es"),
) -> list[uuid.UUID]:
    """Bulk-insert `n` questions (difficulties cycle 1..5 unless fixed) and
    return their ids. Two statements however large n is, for the benchmark."""
    ids = [uuid.uuid4() for _ in range(n)]
    await db.execute(
        insert(Question),
        [
            {
                "id": qid,
                "category_id": category.id,
                "difficulty": difficulty or i % 5 + 1,
                "correct_index": i % 4,
                "status": status,
                "pack_id": pack_id,
                "region": region,
                "content_hash": content_hash(f"Q {qid}"),
            }
            for i, qid in enumerate(ids)
        ],
    )
    await db.execute(
        insert(QuestionTranslation),
        [
            {
                "question_id": qid,
                "locale": loc,
                "stem": f"{loc} {qid}?",
                "options": [f"{qid.hex[:6]}-{k}" for k in range(4)],
            }
            for qid in ids
            for loc in locales
        ],
    )
    return ids


def _request(**overrides) -> dict:
    return {"locale": "en", "question_count": 15, **overrides}


async def _generate(client: AsyncClient, headers, **overrides) -> dict:
    resp = await client.post("/sessions/generate", json=_request(**overrides), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _ids(body: dict) -> list[uuid.UUID]:
    return [uuid.UUID(q["id"]) for q in body["questions"]]


@pytest.mark.asyncio
async def test_requires_a_player(client: AsyncClient, redis):
    resp = await client.post("/sessions/generate", json=_request())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_house_mode_never_returns_pack_or_unlive_questions(
    client: AsyncClient, db: AsyncSession, redis, headers, player, category
):
    """Spec §8: house-pool queries provably never return pack_id IS NOT
    NULL rows. The house pool is exactly 20 live questions, so a 20-draw
    must return exactly those — pack and non-live questions never leak."""
    pack = StudyPack(owner_id=player.id, title="my notes", locale="en")
    db.add(pack)
    await db.flush()
    house = set(await _bank(db, category, 20))
    await _bank(db, category, 20, pack_id=pack.id)
    for status in ("pending", "archived", "rejected"):
        await _bank(db, category, 5, status=status)

    for _ in range(5):
        body = await _generate(client, headers, question_count=20)
        assert set(_ids(body)) == house
        assert body["short_by"] is None


@pytest.mark.asyncio
async def test_study_mode_draws_from_the_pack(
    client: AsyncClient, db: AsyncSession, redis, headers, player, category
):
    pack = StudyPack(owner_id=player.id, title="my notes", locale="en")
    db.add(pack)
    await db.flush()
    await _bank(db, category, 20)
    mine = set(await _bank(db, category, 10, pack_id=pack.id, status="pending"))

    body = await _generate(client, headers, mode="study", pack_id=str(pack.id), question_count=10)
    assert set(_ids(body)) == mine
    session = await db.get(GameSession, uuid.UUID(body["session_id"]))
    assert (session.mode, session.pack_id) == ("study", pack.id)


@pytest.mark.asyncio
async def test_pack_id_must_match_mode(client: AsyncClient, redis, headers):
    no_pack = await client.post(
        "/sessions/generate", json=_request(mode="study"), headers=headers
    )
    house_with_pack = await client.post(
        "/sessions/generate", json=_request(pack_id=str(uuid.uuid4())), headers=headers
    )
    assert (no_pack.status_code, house_with_pack.status_code) == (422, 422)


@pytest.mark.asyncio
async def test_no_duplicates_and_ramp_order(
    client: AsyncClient, db: AsyncSession, redis, headers, category
):
    """A 30-question ramp over a 30-question pool must use every question
    once, ordered easy → hard, and persist the set in that order."""
    pool = set(await _bank(db, category, 30))
    body = await _generate(client, headers, question_count=30)
    ids = _ids(body)
    assert len(ids) == len(set(ids)) == 30 and set(ids) == pool
    assert [q["ordinal"] for q in body["questions"]] == list(range(30))
    difficulty = {q.id: q.difficulty for q in (await db.execute(select(Question))).scalars()}
    curve = [difficulty[i] for i in ids]
    assert curve == sorted(curve)

    rows = (
        await db.execute(
            select(SessionQuestion)
            .where(SessionQuestion.session_id == uuid.UUID(body["session_id"]))
            .order_by(SessionQuestion.ordinal)
        )
    ).scalars().all()
    assert [r.question_id for r in rows] == ids


@pytest.mark.asyncio
async def test_ramp_shares_and_fill_from_other_bands(
    client: AsyncClient, db: AsyncSession, redis, headers, category
):
    """Spec §4: ~30% difficulty 1–2, ~45% difficulty 3, ~25% difficulty 4–5.
    With a deep pool the bands come out as planned; when one band is thin
    the gap is filled from the rest rather than reported as short."""
    assert svc.ramp_sizes(15) == [4, 7, 4]
    assert svc.ramp_sizes(20) == [6, 9, 5]
    for d in range(1, 6):
        await _bank(db, category, 20, difficulty=d)
    difficulty = {q.id: q.difficulty for q in (await db.execute(select(Question))).scalars()}

    body = await _generate(client, headers, question_count=20)
    bands = [difficulty[i] for i in _ids(body)]
    assert (
        sum(d <= 2 for d in bands),
        sum(d == 3 for d in bands),
        sum(d >= 4 for d in bands),
    ) == (6, 9, 5)

    # Only difficulty-3 questions in this category: the ramp still fills.
    only_mid = Category(slug="mid")
    db.add(only_mid)
    await db.flush()
    await _bank(db, only_mid, 15, difficulty=3)
    body = await _generate(client, headers, category_ids=[str(only_mid.id)], question_count=15)
    assert len(_ids(body)) == 15 and body["short_by"] is None


@pytest.mark.asyncio
async def test_short_by_when_the_pool_is_too_small(
    client: AsyncClient, db: AsyncSession, redis, headers, category
):
    pool = set(await _bank(db, category, 7))
    for curve in ("ramp", "flat"):
        body = await _generate(client, headers, question_count=15, difficulty_curve=curve)
        assert set(_ids(body)) == pool and len(_ids(body)) == 7, curve
        assert body["short_by"] == 8

    empty = Category(slug="empty")
    db.add(empty)
    await db.flush()
    body = await _generate(client, headers, category_ids=[str(empty.id)], question_count=15)
    assert body["questions"] == [] and body["short_by"] == 15


@pytest.mark.asyncio
async def test_filters_region_category_and_locale(
    client: AsyncClient, db: AsyncSession, redis, headers, category
):
    other = Category(slug="other")
    db.add(other)
    await db.flush()
    wanted = set(await _bank(db, category, 5, region="us"))
    wanted |= set(await _bank(db, category, 5, region="global"))
    await _bank(db, category, 5, region="latam")
    await _bank(db, other, 5)
    await _bank(db, category, 5, locales=("en",))  # no Spanish text

    body = await _generate(
        client,
        headers,
        locale="es",
        region="us",
        category_ids=[str(category.id)],
        question_count=30,
    )
    assert set(_ids(body)) == wanted
    assert all(q["stem"].startswith("es ") for q in body["questions"])
    assert body["short_by"] == 20


@pytest.mark.asyncio
async def test_options_are_shuffled_per_session_and_stored(
    client: AsyncClient, db: AsyncSession, redis, headers, category
):
    """The client sees a permutation of each question's options and no
    correct_index; session_questions.option_order maps the shown position
    back to the original, and the cached copy carries the shifted
    correct_index so an answer can be scored from either."""
    await _bank(db, category, 15)
    originals = {
        t.question_id: t
        for t in (
            await db.execute(select(QuestionTranslation).where(QuestionTranslation.locale == "en"))
        ).scalars()
    }
    correct = {q.id: q.correct_index for q in (await db.execute(select(Question))).scalars()}

    body = await _generate(client, headers)
    assert len(body["questions"]) == 15
    assert not any("correct_index" in q for q in body["questions"])
    session_id = uuid.UUID(body["session_id"])
    stored = {
        r.ordinal: r
        for r in (
            await db.execute(
                select(SessionQuestion).where(SessionQuestion.session_id == session_id)
            )
        ).scalars()
    }
    cached_raw, ttl = redis.store[f"session:{session_id}:questions"]
    cached = json.loads(cached_raw)
    assert ttl == SESSION_QUESTIONS_TTL == timedelta(hours=2)
    assert (cached["session_id"], cached["locale"]) == (str(session_id), "en")

    for shown, server in zip(body["questions"], cached["questions"]):
        qid = uuid.UUID(shown["id"])
        order = stored[shown["ordinal"]].option_order
        assert sorted(order) == [0, 1, 2, 3]
        assert shown["options"] == [originals[qid].options[j] for j in order]
        assert shown["stem"] == originals[qid].stem
        # Cached copy = what the client saw + where the right answer went.
        assert {k: v for k, v in server.items() if k != "correct_index"} == shown
        assert order[server["correct_index"]] == correct[qid]
        assert shown["options"][server["correct_index"]] == (
            originals[qid].options[correct[qid]]
        )
    # 15 independent shuffles of 4 options: all identity is a 24^-15 event.
    assert any(r.option_order != [0, 1, 2, 3] for r in stored.values())


@pytest.mark.asyncio
async def test_session_row(
    client: AsyncClient, db: AsyncSession, redis, headers, player, category
):
    await _bank(db, category, 15)
    body = await _generate(client, headers, region="latam", category_ids=[str(category.id)])
    session = await db.get(GameSession, uuid.UUID(body["session_id"]))
    assert (session.host_id, session.locale, session.region) == (player.id, "en", "latam")
    assert (session.mode, session.pack_id, session.question_count) == ("house", None, 15)
    assert session.category_ids == [category.id]
    # Phase 2 owns lobby -> running; the draw does not start the game.
    assert session.status == "lobby" and session.started_at is None
    assert len(session.join_code) == svc.JOIN_CODE_LENGTH
    assert set(session.join_code) <= set(svc.JOIN_CODE_ALPHABET)


@pytest.mark.asyncio
async def test_join_code_collision_is_retried(
    client: AsyncClient, db: AsyncSession, redis, headers, category, monkeypatch
):
    await _bank(db, category, 5)
    first = await _generate(client, headers, question_count=5)
    taken = (await db.get(GameSession, uuid.UUID(first["session_id"]))).join_code
    codes = iter([taken, "FRESH1"])
    monkeypatch.setattr(svc, "new_join_code", lambda: next(codes))

    second = await _generate(client, headers, question_count=5)
    session = await db.get(GameSession, uuid.UUID(second["session_id"]))
    assert session.join_code == "FRESH1"
    assert len(session.questions) == 5


@pytest.mark.asyncio
async def test_redis_down_still_creates_the_session(
    client: AsyncClient, db: AsyncSession, redis, headers, category
):
    await _bank(db, category, 15)
    redis.down = True
    body = await _generate(client, headers)
    assert len(body["questions"]) == 15
    assert await db.get(GameSession, uuid.UUID(body["session_id"])) is not None
    assert redis.store == {}


@pytest.mark.asyncio
async def test_generate_under_200ms_with_5000_live_questions(
    client: AsyncClient, db: AsyncSession, redis, headers, category
):
    """Spec §8: 15 non-duplicate live questions in under 200ms with 5,000
    questions in the bank. Timed through the ASGI client, after one
    warm-up call so SQLAlchemy's statement cache is not what is measured."""
    await _bank(db, category, 5_000)
    await _bank(db, category, 500, status="pending")
    await _generate(client, headers, locale="es")

    started = time.perf_counter()
    body = await _generate(client, headers)
    elapsed = time.perf_counter() - started

    ids = _ids(body)
    assert len(ids) == len(set(ids)) == 15 and body["short_by"] is None
    print(f"\n5,000-question draw: {elapsed * 1000:.0f} ms")
    assert elapsed < 0.2, f"took {elapsed * 1000:.0f} ms"
