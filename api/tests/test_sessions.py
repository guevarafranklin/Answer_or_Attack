"""Phase 2 §10 step 3: POST /sessions makes a lobby; the draw at start
builds per-category pools and the block reserve (spec §6)."""
import json
import time
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import PLAYER_HEADER
from app.cache import SESSION_QUESTIONS_TTL
from app.game.config import GameConfig
from app.game import seed as seed_mod
from app.game.engine import Question as EngineQuestion
from app.models import (
    Category,
    GameSession,
    Question,
    QuestionTranslation,
    SessionQuestion,
    StudyPack,
    User,
)
from app.models.sessions import BLOCK_POOL
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
    return await _category(db)


async def _category(db: AsyncSession, slug: str | None = None) -> Category:
    cat = Category(slug=slug or f"c-{uuid.uuid4().hex[:6]}")
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


async def _difficulties(db: AsyncSession) -> dict[uuid.UUID, int]:
    return {q.id: q.difficulty for q in (await db.execute(select(Question))).scalars()}


def _request(**overrides) -> dict:
    """Request body; `question_count`/`max_players` shortcuts go into
    config_overrides (question_count defaults to 15)."""
    config = {"question_count": overrides.pop("question_count", 15)}
    if "max_players" in overrides:
        config["max_players"] = overrides.pop("max_players")
    config.update(overrides.pop("config_overrides", {}))
    return {"locale": "en", "config_overrides": config, **overrides}


async def _create(client: AsyncClient, headers, **overrides) -> dict:
    resp = await client.post("/sessions", json=_request(**overrides), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _lobby(db: AsyncSession, host: User, **overrides) -> GameSession:
    req = svc.SessionCreateRequest(**_request(**overrides))
    return await svc.create_session(db, req, host)


async def _draw(db: AsyncSession, session: GameSession, present: int = 4, **kw) -> svc.Draw:
    return await svc.draw_at_start(db, session, present_count=present, **kw)


def _all_ids(draw: svc.Draw) -> list[str]:
    return [q.id for qs in draw.pools.values() for q in qs] + [q.id for q in draw.block_reserve]


async def _stored(db: AsyncSession, session_id: uuid.UUID) -> list[SessionQuestion]:
    rows = await db.execute(
        select(SessionQuestion)
        .where(SessionQuestion.session_id == session_id)
        .order_by(SessionQuestion.ordinal)
    )
    return list(rows.scalars())


# ---------- POST /sessions ----------


@pytest.mark.asyncio
async def test_requires_a_player(client: AsyncClient):
    resp = await client.post("/sessions", json=_request())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_create_makes_a_lobby_without_questions(
    client: AsyncClient, db: AsyncSession, headers, player, category
):
    """The draw happens at start, not here: a fresh lobby has no rows in
    session_questions even with a full bank available."""
    await _bank(db, category, 30)
    body = await _create(client, headers, region="latam", category_ids=[str(category.id)])
    session = await db.get(GameSession, uuid.UUID(body["session_id"]))
    assert body == {"session_id": str(session.id), "join_code": session.join_code}
    assert (session.host_id, session.locale, session.region) == (player.id, "en", "latam")
    assert (session.mode, session.pack_id, session.question_count) == ("house", None, 15)
    assert session.category_ids == [category.id]
    assert session.status == "lobby" and session.started_at is None
    assert len(session.join_code) == svc.JOIN_CODE_LENGTH
    assert set(session.join_code) <= set(svc.JOIN_CODE_ALPHABET)
    assert await _stored(db, session.id) == []


@pytest.mark.asyncio
async def test_config_overrides_are_validated_and_stored(
    client: AsyncClient, db: AsyncSession, headers
):
    body = await _create(client, headers, config_overrides={"question_count": 20, "max_tokens": 3})
    session = await db.get(GameSession, uuid.UUID(body["session_id"]))
    assert session.config_overrides == {"question_count": 20, "max_tokens": 3}
    assert session.question_count == 20  # derived from the overrides
    assert session.resolved_config is None and session.rng_seed is None

    plain = await client.post("/sessions", json={"locale": "en"}, headers=headers)
    assert plain.status_code == 201
    assert (await db.get(GameSession, uuid.UUID(plain.json()["session_id"]))).config_overrides == {}

    for bad in ({"questoin_count": 20}, {"question_count": 0}, {"starting_xp_choices": []}):
        resp = await client.post(
            "/sessions", json={"locale": "en", "config_overrides": bad}, headers=headers
        )
        assert resp.status_code == 422, bad
        assert resp.json()["detail"].startswith("config_overrides: ")
    assert await db.scalar(select(func.count()).select_from(GameSession)) == 2


@pytest.mark.asyncio
async def test_pack_id_must_match_mode(client: AsyncClient, headers):
    no_pack = await client.post("/sessions", json=_request(mode="study"), headers=headers)
    house_with_pack = await client.post(
        "/sessions", json=_request(pack_id=str(uuid.uuid4())), headers=headers
    )
    assert (no_pack.status_code, house_with_pack.status_code) == (422, 422)


@pytest.mark.asyncio
async def test_join_code_collision_is_retried(
    client: AsyncClient, db: AsyncSession, headers, monkeypatch
):
    first = await _create(client, headers)
    taken = (await db.get(GameSession, uuid.UUID(first["session_id"]))).join_code
    codes = iter([taken, "FRESH1"])
    monkeypatch.setattr(svc, "new_join_code", lambda: next(codes))

    second = await _create(client, headers)
    assert second["join_code"] == "FRESH1"


# ---------- the draw ----------


def test_pool_size_is_ceil_split_plus_two():
    assert svc.pool_size(15, 1) == 17
    assert svc.pool_size(15, 4) == 6  # ceil(3.75) + 2
    assert svc.pool_size(15, 5) == 5
    assert svc.pool_size(16, 5) == 6
    assert svc.ramp_sizes(15) == [4, 7, 4]
    assert svc.ramp_sizes(20) == [6, 9, 5]
    assert svc.ramp_sizes(5) == [2, 2, 1]


@pytest.mark.asyncio
async def test_pools_per_category_ramped_and_reserve_of_max_players(
    db: AsyncSession, player
):
    """Five categories with a deep bank, question_count 15, max_players 8:
    five pools of ceil(15/5)+2 = 5 questions each, ramped easy → hard,
    and a reserve of 8 difficulty-2–3 questions. Nothing is drawn twice
    and every question comes from a selected category."""
    cats = [await _category(db) for _ in range(5)]
    banks = {c.id: set(await _bank(db, c, 40)) for c in cats}
    await _bank(db, await _category(db, "unselected"), 40)
    session = await _lobby(db, player, category_ids=[str(c.id) for c in cats], max_players=8)
    difficulty = await _difficulties(db)

    draw = await _draw(db, session)
    assert set(draw.pools) == {str(c.id) for c in cats}
    for cid, pool in draw.pools.items():
        assert len(pool) == 5
        assert {uuid.UUID(q.id) for q in pool} <= banks[uuid.UUID(cid)]
        assert all(q.category_id == cid for q in pool)
        curve = [q.difficulty for q in pool]
        assert curve == sorted(curve) == [difficulty[uuid.UUID(q.id)] for q in pool]
        assert (sum(d <= 2 for d in curve), sum(d == 3 for d in curve), sum(d >= 4 for d in curve)) == (2, 2, 1)
    assert len(draw.block_reserve) == 8
    assert all(q.difficulty in (2, 3) for q in draw.block_reserve)
    assert {uuid.UUID(q.id) for q in draw.block_reserve} <= set().union(*banks.values())
    ids = _all_ids(draw)
    assert len(ids) == len(set(ids)) == 5 * 5 + 8
    assert draw.short_by is None


@pytest.mark.asyncio
async def test_rows_carry_pool_and_ordinals_run_through_pools_then_reserve(
    db: AsyncSession, player
):
    cats = [await _category(db) for _ in range(3)]
    for c in cats:
        await _bank(db, c, 20)
    session = await _lobby(
        db, player, question_count=9, category_ids=[str(c.id) for c in cats], max_players=4
    )

    draw = await _draw(db, session)
    rows = await _stored(db, session.id)
    assert [r.ordinal for r in rows] == list(range(3 * 5 + 4))
    expected = [(str(cid), uuid.UUID(q.id)) for cid, qs in draw.pools.items() for q in qs]
    expected += [(BLOCK_POOL, uuid.UUID(q.id)) for q in draw.block_reserve]
    assert [(r.pool, r.question_id) for r in rows] == expected
    # Cache and rows agree on ordinal, pool and difficulty.
    assert [(q.ordinal, q.pool) for q in draw.cache.questions] == [(r.ordinal, r.pool) for r in rows]


@pytest.mark.asyncio
async def test_engine_questions_match_the_stored_shuffle(db: AsyncSession, player, category):
    """`correct_option` in the engine's Question is the position after the
    per-session shuffle: option_order[correct_option] is the bank's
    correct_index, and the cached options are the originals permuted."""
    await _bank(db, category, 60)  # 12 per difficulty: room for a full reserve
    originals = {
        t.question_id: t
        for t in (
            await db.execute(select(QuestionTranslation).where(QuestionTranslation.locale == "en"))
        ).scalars()
    }
    correct = {q.id: q.correct_index for q in (await db.execute(select(Question))).scalars()}
    session = await _lobby(db, player, max_players=4)

    draw = await _draw(db, session)
    stored = {r.ordinal: r for r in await _stored(db, session.id)}
    engine = {q.id: q for q in draw.pools[str(category.id)] + draw.block_reserve}
    assert len(engine) == 17 + 4
    for cached in draw.cache.questions:
        qid = cached.id
        order = stored[cached.ordinal].option_order
        assert sorted(order) == [0, 1, 2, 3]
        assert cached.options == [originals[qid].options[j] for j in order]
        assert cached.stem == originals[qid].stem
        assert order[cached.correct_index] == correct[qid]
        eq = engine[str(qid)]
        assert eq == EngineQuestion(str(qid), str(category.id), cached.difficulty, cached.correct_index)
    # 21 independent shuffles of 4 options: all identity is a 24^-21 event.
    assert any(r.option_order != [0, 1, 2, 3] for r in stored.values())


@pytest.mark.asyncio
async def test_start_writes_seed_and_resolved_config(db: AsyncSession, player, category):
    """The draw resolves the config for the players present (8 → the
    4–8 starting-XP tier) on top of the host's overrides, picks a seed,
    and stores both on the row; the Draw carries the same."""
    await _bank(db, category, 30)
    session = await _lobby(db, player, config_overrides={"question_count": 5, "attack_cost": 2})
    assert session.resolved_config is None and session.rng_seed is None

    draw = await _draw(db, session, present=8)
    assert session.rng_seed == draw.seed and 0 <= draw.seed < 2**63
    expected = GameConfig.from_overrides(
        {"question_count": 5, "attack_cost": 2}, starting_xp_choices=(10, 12, 15)
    )
    assert draw.config == expected
    assert session.resolved_config == json.loads(json.dumps(expected.summary()))
    assert GameConfig.from_overrides(session.resolved_config) == expected
    assert session.config_overrides == {"question_count": 5, "attack_cost": 2}  # untouched

    bigger = await _draw(db, await _lobby(db, player), present=9)
    assert bigger.config.starting_xp_choices == (10, 11, 13)
    pinned = await _draw(
        db, await _lobby(db, player, config_overrides={"starting_xp_choices": [10, 18, 30]}), present=9
    )
    assert pinned.config.starting_xp_choices == (10, 18, 30)


@pytest.mark.asyncio
async def test_seed_fixes_the_option_shuffle(db: AsyncSession, player, category):
    """Given the seed, the shuffle is reproducible (the questions come
    from the DB's random(), and session_questions records which); the
    engine stream from the same seed is independent of the draw's."""
    await _bank(db, category, 10)
    a = await _draw(db, await _lobby(db, player, question_count=1, max_players=2), seed=7)
    b = await _draw(db, await _lobby(db, player, question_count=1, max_players=2), seed=7)
    assert a.seed == b.seed == 7
    orders_a = [r.option_order for r in await _stored(db, a.cache.session_id)]
    orders_b = [r.option_order for r in await _stored(db, b.cache.session_id)]
    assert orders_a == orders_b and len(orders_a) == 3 + 2
    rng = seed_mod.draw_rng(7)
    assert orders_a == [svc.shuffle_options(rng) for _ in range(5)]
    assert seed_mod.engine_rng(7).random() != seed_mod.draw_rng(7).random()
    assert seed_mod.engine_rng(7).random() == seed_mod.engine_rng(7).random()


@pytest.mark.asyncio
async def test_empty_category_ids_means_every_category_with_questions(
    db: AsyncSession, player
):
    cats = [await _category(db) for _ in range(4)]
    for c in cats[:3]:
        await _bank(db, c, 20)
    await _bank(db, cats[3], 20, status="pending")  # nothing eligible here
    session = await _lobby(db, player, question_count=15, max_players=4)

    draw = await _draw(db, session)
    assert set(draw.pools) == {str(c.id) for c in cats[:3]}
    assert all(len(pool) == 7 for pool in draw.pools.values())  # ceil(15/3) + 2
    assert session.category_ids == []  # the row keeps what the host asked for


@pytest.mark.asyncio
async def test_house_mode_never_draws_pack_or_unlive_questions(
    db: AsyncSession, player, category
):
    """Phase 1 §8, still true: the house pool is exactly the 20 live house
    questions, so a draw wanting more than 20 returns exactly those."""
    pack = StudyPack(owner_id=player.id, title="my notes", locale="en")
    db.add(pack)
    await db.flush()
    house = set(await _bank(db, category, 20))
    await _bank(db, category, 20, pack_id=pack.id)
    for status in ("pending", "archived", "rejected"):
        await _bank(db, category, 5, status=status)

    for _ in range(3):
        draw = await _draw(db, await _lobby(db, player, question_count=30, max_players=20))
        assert {uuid.UUID(i) for i in _all_ids(draw)} == house


@pytest.mark.asyncio
async def test_study_mode_draws_from_the_pack(db: AsyncSession, player, category):
    pack = StudyPack(owner_id=player.id, title="my notes", locale="en")
    db.add(pack)
    await db.flush()
    await _bank(db, category, 20)
    mine = set(await _bank(db, category, 10, pack_id=pack.id, status="pending"))

    session = await _lobby(
        db, player, mode="study", pack_id=str(pack.id), question_count=10, max_players=20
    )
    draw = await _draw(db, session)
    assert {uuid.UUID(i) for i in _all_ids(draw)} == mine
    assert len(draw.pools[str(category.id)]) == 10  # wanted 12: short by 2, reserve empty
    assert draw.block_reserve == [] and draw.short_by == 2


@pytest.mark.asyncio
async def test_thin_band_is_filled_from_the_rest_of_the_category(
    db: AsyncSession, player, category
):
    """Only difficulty-3 questions in the category: the ramp still fills
    the pool; and a reserve draw never steals from a pool."""
    await _bank(db, category, 30, difficulty=3)
    session = await _lobby(db, player, question_count=15, max_players=20)
    draw = await _draw(db, session)
    assert len(draw.pools[str(category.id)]) == 17 and draw.short_by is None
    assert len(draw.block_reserve) == 13  # the 30 - 17 left over
    ids = _all_ids(draw)
    assert len(ids) == len(set(ids)) == 30


@pytest.mark.asyncio
async def test_reserve_takes_only_difficulty_2_and_3(db: AsyncSession, player, category):
    for d in (1, 4, 5):
        await _bank(db, category, 10, difficulty=d)
    mid = set(await _bank(db, category, 3, difficulty=2)) | set(await _bank(db, category, 3, difficulty=3))
    session = await _lobby(db, player, question_count=1, max_players=20)  # pool of 3
    draw = await _draw(db, session)
    pool_ids = {uuid.UUID(q.id) for q in draw.pools[str(category.id)]}
    reserve_ids = {uuid.UUID(q.id) for q in draw.block_reserve}
    assert reserve_ids <= mid and not (reserve_ids & pool_ids)
    assert len(reserve_ids) == len(mid - pool_ids)


@pytest.mark.asyncio
async def test_short_by_counts_the_pools_shortfall(db: AsyncSession, player):
    """Two categories, one with 3 questions and one with none: pools of 7
    wanted 14, got 3, short by 11. The reserve's own shortfall is not
    reported (the engine falls back to the pools)."""
    thin, empty = await _category(db), await _category(db)
    ids = set(await _bank(db, thin, 3, locales=("en",)))
    session = await _lobby(
        db, player, question_count=10, category_ids=[str(thin.id), str(empty.id)], max_players=20
    )
    draw = await _draw(db, session)
    assert {uuid.UUID(q.id) for q in draw.pools[str(thin.id)]} == ids
    assert draw.pools[str(empty.id)] == []
    assert draw.block_reserve == []
    assert draw.short_by == draw.cache.short_by == 11

    nothing = await _draw(db, await _lobby(db, player, category_ids=[str(empty.id)]))
    assert nothing.pools == {str(empty.id): []} and nothing.short_by == 17

    # No category has an eligible question in Spanish: no pools at all.
    no_categories = await _draw(db, await _lobby(db, player, locale="es"))
    assert no_categories.pools == {} and no_categories.short_by == 15


@pytest.mark.asyncio
async def test_filters_region_and_locale(db: AsyncSession, player, category):
    other = await _category(db, "other")
    wanted = set(await _bank(db, category, 5, region="us"))
    wanted |= set(await _bank(db, category, 5, region="global"))
    await _bank(db, category, 5, region="latam")
    await _bank(db, other, 5)
    await _bank(db, category, 5, locales=("en",))  # no Spanish text

    session = await _lobby(
        db,
        player,
        locale="es",
        region="us",
        category_ids=[str(category.id)],
        question_count=30,
        max_players=20,
    )
    draw = await _draw(db, session)
    assert {uuid.UUID(i) for i in _all_ids(draw)} == wanted
    assert all(q.stem.startswith("es ") for q in draw.cache.questions)
    assert draw.cache.locale == "es"


@pytest.mark.asyncio
async def test_draw_happens_once(db: AsyncSession, player, category):
    await _bank(db, category, 60)
    session = await _lobby(db, player, max_players=4)
    await _draw(db, session)
    with pytest.raises(svc.AlreadyDrawn):
        await _draw(db, session)
    assert len(await _stored(db, session.id)) == 21


@pytest.mark.asyncio
async def test_cache_payload_and_redis_down(db: AsyncSession, player, category):
    await _bank(db, category, 60)
    draw = await _draw(db, await _lobby(db, player, max_players=4))
    redis = FakeRedis()

    assert await svc.cache_questions(redis, draw.cache) is True
    raw, ttl = redis.store[f"session:{draw.cache.session_id}:questions"]
    assert ttl == SESSION_QUESTIONS_TTL == timedelta(hours=2)
    cached = json.loads(raw)
    assert (cached["session_id"], cached["locale"], cached["short_by"]) == (
        str(draw.cache.session_id), "en", None
    )
    assert len(cached["questions"]) == 21
    assert {q["pool"] for q in cached["questions"]} == {str(category.id), BLOCK_POOL}
    assert set(cached["questions"][0]) == {
        "id", "stem", "options", "ordinal", "correct_index", "pool", "difficulty"
    }

    redis.down = True
    assert await svc.cache_questions(redis, draw.cache) is False  # logged, not raised


@pytest.mark.asyncio
async def test_draw_under_200ms_with_5000_live_questions(db: AsyncSession, player):
    """Phase 1 §8's budget, kept for the new shape: 5,000 live questions
    over 5 categories, pools + reserve drawn in under 200ms after one
    warm-up so SQLAlchemy's statement cache is not what is measured."""
    cats = [await _category(db) for _ in range(5)]
    for c in cats:
        await _bank(db, c, 1_000)
        await _bank(db, c, 100, status="pending")
    await _draw(db, await _lobby(db, player, locale="es"))

    session = await _lobby(db, player)
    started = time.perf_counter()
    draw = await _draw(db, session)
    elapsed = time.perf_counter() - started

    ids = _all_ids(draw)
    assert len(ids) == len(set(ids)) == 5 * 5 + 20 and draw.short_by is None
    print(f"\n5,000-question draw: {elapsed * 1000:.0f} ms")
    assert elapsed < 0.2, f"took {elapsed * 1000:.0f} ms"
