"""Sessions (Phase 2 spec §4, §6): create the lobby, draw the questions at
start.

`create_session` only makes the lobby row with its join code and the
host's validated `config_overrides`. The draw happens when the host
starts the game, not at lobby creation, so a lobby that never starts
never touches the bank. `draw_at_start` resolves the GameConfig for the
players present (the starting-XP tier), picks the session's rng seed,
builds the per-category pools and the block reserve, persists them to
session_questions with the per-session option shuffle and the pool each
row belongs to, and returns the engine-shaped questions plus the payload
for Redis (`cache_questions` stores it; the caller commits in between).
Everything random on the Python side comes from app.game.seed streams of
`sessions.rng_seed`; the question pick itself is the database's
random(), and the picked set is what session_questions records.

The draw (§6):

* One pool per selected category, `ceil(question_count / categories) + 2`
  questions each, ramped easy → hard: the three RAMP_BANDS are drawn
  separately and a thin band is topped up from any difficulty left in the
  category. Empty `category_ids` means every category with an eligible
  question.
* A block reserve of `max_players` questions of difficulty 2–3 from any
  selected category, drawn after the pools so nothing is in both.
* Eligibility is Phase 1's: the locale's translation joined to the
  question, house = live house content, study = the pack, region IN
  (:region, 'global').
* `short_by` counts the questions the pools wanted and could not get. The
  reserve is best effort (the engine falls back to the pools when it runs
  dry), so its shortfall is not reported.

Three to five queries however many categories there are: the ranked band
draw, the fill (only when a band was thin), the reserve, and the category
list when none was given.
"""
from __future__ import annotations

import json
import logging
import math
import random
import secrets
import uuid
from collections import defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import ColumnElement, and_, case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import SESSION_QUESTIONS_TTL, session_questions_key
from app.db import constraint_name
from app.game.config import ConfigError, GameConfig
from app.game.engine import Question as EngineQuestion
from app.game.seed import draw_rng, new_seed
from app.models import GameSession, Question, QuestionTranslation, SessionQuestion, User
from app.models.sessions import BLOCK_POOL
from app.schemas.sessions import (
    SessionCreateRequest,
    SessionQuestionsCache,
    SessionQuestionServer,
)

log = logging.getLogger(__name__)

# Ramp within a pool: roughly 30% difficulty 1–2, 45% difficulty 3, 25%
# difficulty 4–5 (Phase 1 §4, kept per pool in Phase 2).
RAMP_BANDS: tuple[tuple[tuple[int, int], float], ...] = (
    ((1, 2), 0.30),
    ((3, 3), 0.45),
    ((4, 5), 0.25),
)
POOL_SLACK = 2  # extra questions per pool over the even split (§6)
BLOCK_DIFFICULTY = (2, 3)
JOIN_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
JOIN_CODE_LENGTH = 6
_JOIN_CODE_ATTEMPTS = 10


class PackRequired(Exception):
    """study mode needs a pack_id; house mode must not have one."""


class AlreadyDrawn(Exception):
    """The session already has its questions; the draw happens once."""


class BadConfig(Exception):
    """config_overrides did not pass GameConfig.from_overrides."""

    def __init__(self, error: ConfigError) -> None:
        super().__init__(str(error))


@dataclass(frozen=True)
class _Drawn:
    id: uuid.UUID
    category_id: uuid.UUID
    difficulty: int
    correct_index: int
    stem: str
    options: list[str]


@dataclass
class Draw:
    """What `draw_at_start` produced: the resolved config and seed the game
    runs with, the engine's inputs, and the cache payload, which carries
    the same questions with their text."""

    config: GameConfig
    seed: int
    pools: dict[str, list[EngineQuestion]] = field(default_factory=dict)
    block_reserve: list[EngineQuestion] = field(default_factory=list)
    cache: SessionQuestionsCache | None = None

    @property
    def short_by(self) -> int | None:
        return self.cache.short_by if self.cache else None


# ---------- lobby ----------


def new_join_code() -> str:
    return "".join(secrets.choice(JOIN_CODE_ALPHABET) for _ in range(JOIN_CODE_LENGTH))


def config_from(overrides: dict[str, Any], **kwargs: Any) -> GameConfig:
    try:
        return GameConfig.from_overrides(overrides, **kwargs)
    except ConfigError as exc:
        raise BadConfig(exc) from exc


async def create_session(db: AsyncSession, req: SessionCreateRequest, host: User) -> GameSession:
    """The lobby row under a fresh join code (flush, no commit). No
    questions yet: `draw_at_start` draws them when the host starts.
    Raises BadConfig when the overrides do not make a valid GameConfig."""
    _check_mode(req.mode, req.pack_id)
    config = config_from(req.config_overrides)
    session = GameSession(
        host_id=host.id,
        locale=req.locale,
        region=req.region,
        mode=req.mode,
        pack_id=req.pack_id,
        category_ids=req.category_ids,
        question_count=config.question_count,
        config_overrides=req.config_overrides,
        status="lobby",  # the runtime owns lobby -> running (§6)
    )
    await _insert_with_join_code(db, session)
    return session


def _check_mode(mode: str, pack_id: uuid.UUID | None) -> None:
    if (mode == "study") != (pack_id is not None):
        raise PackRequired()


async def _insert_with_join_code(db: AsyncSession, session: GameSession) -> None:
    """Flush the session under a fresh join code, retrying on a collision
    (32^6 codes, so a retry is rare; the loop just makes it impossible to
    hand two lobbies the same code)."""
    for _ in range(_JOIN_CODE_ATTEMPTS):
        session.join_code = new_join_code()
        try:
            async with db.begin_nested():
                db.add(session)
                await db.flush()
            return
        except IntegrityError as exc:
            if constraint_name(exc) != "sessions_join_code_key":
                raise
            if session in db:
                db.expunge(session)
    raise RuntimeError("could not allocate a unique join code")


# ---------- eligibility ----------


def _pool_filter(session: GameSession, category_ids: Collection[uuid.UUID]) -> ColumnElement[bool]:
    if session.mode == "study":
        where = Question.pack_id == session.pack_id
    else:
        # House pool: live, and never a pack question (Phase 1 §8 insists on
        # this one; tests/test_sessions.py proves it).
        where = and_(Question.status == "live", Question.pack_id.is_(None))
    where = and_(where, Question.region.in_((session.region, "global")))
    if category_ids:
        where = and_(where, Question.category_id.in_(list(category_ids)))
    return where


def _eligible(session: GameSession, category_ids: Collection[uuid.UUID]):
    return (
        select(
            Question.id,
            Question.category_id,
            Question.difficulty,
            Question.correct_index,
            QuestionTranslation.stem,
            QuestionTranslation.options,
        )
        .join(
            QuestionTranslation,
            and_(
                QuestionTranslation.question_id == Question.id,
                QuestionTranslation.locale == session.locale,
            ),
        )
        .where(_pool_filter(session, category_ids))
    )


async def _categories_with_questions(db: AsyncSession, session: GameSession) -> list[uuid.UUID]:
    """Every category with at least one eligible question, in a stable
    order (the pools' ordinal order)."""
    rows = await db.execute(
        select(Question.category_id)
        .join(
            QuestionTranslation,
            and_(
                QuestionTranslation.question_id == Question.id,
                QuestionTranslation.locale == session.locale,
            ),
        )
        .where(_pool_filter(session, ()))
        .distinct()
        .order_by(Question.category_id)
    )
    return list(rows.scalars())


# ---------- the draw ----------


def pool_size(question_count: int, categories: int) -> int:
    """ceil(question_count / categories) + POOL_SLACK (§6)."""
    return math.ceil(question_count / categories) + POOL_SLACK


def ramp_sizes(count: int) -> list[int]:
    """Band sizes for a ramp of `count` questions; the middle band absorbs
    the rounding so they always sum to `count`."""
    low = round(count * RAMP_BANDS[0][1])
    high = round(count * RAMP_BANDS[2][1])
    return [low, count - low - high, high]


def _band_expr():
    """0, 1 or 2: which RAMP_BANDS band a question's difficulty falls in."""
    low, mid, _ = (band for band, _ in RAMP_BANDS)
    return case(
        (Question.difficulty.between(*low), 0),
        (Question.difficulty.between(*mid), 1),
        else_=2,
    )


async def _draw_bands(
    db: AsyncSession, session: GameSession, category_ids: Collection[uuid.UUID], size: int
) -> dict[uuid.UUID, list[_Drawn]]:
    """Per category, up to ramp_sizes(size) random questions from each
    band, in one ranked query."""
    sizes = ramp_sizes(size)
    band = _band_expr().label("band")
    rank = (
        func.row_number()
        .over(partition_by=(Question.category_id, band), order_by=func.random())
        .label("rank")
    )
    ranked = _eligible(session, category_ids).add_columns(band, rank).subquery()
    quota = case((ranked.c.band == 0, sizes[0]), (ranked.c.band == 1, sizes[1]), else_=sizes[2])
    rows = await db.execute(select(ranked).where(ranked.c.rank <= quota))
    pools: dict[uuid.UUID, list[_Drawn]] = defaultdict(list)
    for row in rows:
        pools[row.category_id].append(_Drawn(*row[:6]))
    return pools


async def _fill(
    db: AsyncSession,
    session: GameSession,
    needed: dict[uuid.UUID, int],
    exclude: Collection[uuid.UUID],
) -> dict[uuid.UUID, list[_Drawn]]:
    """Top up thin pools from any difficulty left in their category: one
    ranked query for the largest gap, trimmed per category."""
    rank = (
        func.row_number()
        .over(partition_by=Question.category_id, order_by=func.random())
        .label("rank")
    )
    ranked = (
        _eligible(session, list(needed))
        .where(Question.id.not_in(list(exclude)))
        .add_columns(rank)
        .subquery()
    )
    rows = await db.execute(select(ranked).where(ranked.c.rank <= max(needed.values())))
    extra: dict[uuid.UUID, list[_Drawn]] = defaultdict(list)
    for row in rows:
        if len(extra[row.category_id]) < needed[row.category_id]:
            extra[row.category_id].append(_Drawn(*row[:6]))
    return extra


async def _draw_reserve(
    db: AsyncSession,
    session: GameSession,
    category_ids: Collection[uuid.UUID],
    limit: int,
    exclude: Collection[uuid.UUID],
) -> list[_Drawn]:
    if limit <= 0:
        return []
    query = (
        _eligible(session, category_ids)
        .where(Question.difficulty.between(*BLOCK_DIFFICULTY))
        .where(Question.id.not_in(list(exclude)))
        .order_by(func.random())
        .limit(limit)
    )
    return [_Drawn(*row) for row in await db.execute(query)]


async def draw_pools(
    db: AsyncSession, session: GameSession, *, reserve_size: int
) -> tuple[dict[uuid.UUID, list[_Drawn]], list[_Drawn], int]:
    """The raw draw: pools keyed by category (each ramped easy → hard,
    random within a band), the block reserve, and the pools' shortfall.
    No question id appears twice."""
    category_ids = list(session.category_ids) or await _categories_with_questions(db, session)
    if not category_ids:
        return {}, [], session.question_count
    size = pool_size(session.question_count, len(category_ids))

    pools = await _draw_bands(db, session, category_ids, size)
    needed = {cid: size - len(pools.get(cid, [])) for cid in category_ids}
    needed = {cid: n for cid, n in needed.items() if n > 0}
    if needed:
        taken = {q.id for qs in pools.values() for q in qs}
        for cid, extra in (await _fill(db, session, needed, taken)).items():
            pools[cid].extend(extra)

    ordered = {
        cid: sorted(pools.get(cid, []), key=lambda q: q.difficulty)  # stable within a band
        for cid in category_ids
    }
    taken = {q.id for qs in ordered.values() for q in qs}
    reserve = await _draw_reserve(db, session, category_ids, reserve_size, taken)
    short_by = sum(size - len(qs) for qs in ordered.values())
    return ordered, reserve, short_by


def shuffle_options(rng: random.Random) -> list[int]:
    """A permutation p: shown option i is original option p[i]."""
    order = list(range(4))
    rng.shuffle(order)
    return order


def resolve_config(session: GameSession, present_count: int) -> GameConfig:
    """The GameConfig the game runs with: the host's overrides, with
    starting_xp_choices fixed by the player-count tier (an explicit
    override still wins, see GameConfig.choices_for)."""
    config = config_from(session.config_overrides)
    return config_from(
        session.config_overrides, starting_xp_choices=config.choices_for(present_count)
    )


async def draw_at_start(
    db: AsyncSession,
    session: GameSession,
    *,
    present_count: int,
    seed: int | None = None,
) -> Draw:
    """Start-time draw (flush, no commit): resolve the config for the
    players present, pick the rng seed (or take `seed`, for replays and
    tests), write both to the session row, then draw the pools and the
    reserve and return them in the engine's shape plus the cache payload.
    Pools use the session's question_count and categories; the reserve
    holds `max_players` questions. Ordinals run through the pools in
    category order, then the reserve. Raises AlreadyDrawn on a second
    call."""
    if session.rng_seed is not None or await db.scalar(
        select(func.count()).select_from(SessionQuestion).where(SessionQuestion.session_id == session.id)
    ):
        raise AlreadyDrawn()
    config = resolve_config(session, present_count)
    seed = new_seed() if seed is None else seed
    # JSON-shaped (tuples → lists) so the row reads back the same in memory.
    session.resolved_config = json.loads(json.dumps(config.summary()))
    session.rng_seed = seed
    rng = draw_rng(seed)
    pools, reserve, short_by = await draw_pools(db, session, reserve_size=config.max_players)

    draw = Draw(config=config, seed=seed)
    payload: list[SessionQuestionServer] = []
    labelled: Iterable[tuple[str, list[_Drawn]]] = [
        *((str(cid), qs) for cid, qs in pools.items()),
        (BLOCK_POOL, reserve),
    ]
    ordinal = 0
    for pool, drawn in labelled:
        for q in drawn:
            order = shuffle_options(rng)
            correct = order.index(q.correct_index)
            db.add(
                SessionQuestion(
                    session_id=session.id,
                    ordinal=ordinal,
                    question_id=q.id,
                    option_order=order,
                    pool=pool,
                )
            )
            payload.append(
                SessionQuestionServer(
                    id=q.id,
                    stem=q.stem,
                    options=[q.options[j] for j in order],
                    correct_index=correct,
                    ordinal=ordinal,
                    pool=pool,
                    difficulty=q.difficulty,
                )
            )
            engine_q = EngineQuestion(str(q.id), str(q.category_id), q.difficulty, correct)
            if pool == BLOCK_POOL:
                draw.block_reserve.append(engine_q)
            else:
                draw.pools.setdefault(pool, []).append(engine_q)
            ordinal += 1
    for cid in pools:  # a category that came up empty still gets its (empty) pool
        draw.pools.setdefault(str(cid), [])
    await db.flush()
    draw.cache = SessionQuestionsCache(
        session_id=session.id, locale=session.locale, questions=payload, short_by=short_by or None
    )
    return draw


async def cache_questions(redis: Redis, cache: SessionQuestionsCache) -> bool:
    """Store the server-side payload at session:{id}:questions. Call after
    the commit, so the cache never points at a session that does not
    exist. Redis being down is logged, not raised: the DB rows are the
    source of truth and a miss costs one query."""
    try:
        await redis.set(
            session_questions_key(cache.session_id),
            cache.model_dump_json(),
            ex=SESSION_QUESTIONS_TTL,
        )
    except Exception:
        log.warning("could not cache questions for session %s", cache.session_id, exc_info=True)
        return False
    return True
