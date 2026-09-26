"""Session content (spec §4): POST /sessions/generate.

The whole question set is drawn once, at session start, and persisted to
session_questions with the per-session option shuffle; the game never
queries the bank mid-round. `generate_session` builds the rows and the
server-side payload (the one with correct_index) — the router commits,
caches the payload in Redis, and strips correct_index for the client.

Pool: the locale's translation joined to the question, filtered by mode
(house = live house content, study = the pack), region IN (:region,
'global') and the requested categories. The ramp curve draws its three
difficulty bands separately, tops up from any difficulty when a band is
thin, and orders easy → hard; flat is one random draw in random order.
`short_by` is set only when the whole pool is exhausted.
"""
import random
import secrets
import uuid
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import constraint_name
from app.models import GameSession, Question, QuestionTranslation, SessionQuestion, User
from app.schemas.sessions import (
    SessionGenerateRequest,
    SessionQuestionsCache,
    SessionQuestionServer,
)

# §4 ramp: roughly 30% difficulty 1–2, 45% difficulty 3, 25% difficulty 4–5.
RAMP_BANDS: tuple[tuple[tuple[int, int], float], ...] = (
    ((1, 2), 0.30),
    ((3, 3), 0.45),
    ((4, 5), 0.25),
)
JOIN_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
JOIN_CODE_LENGTH = 6
_JOIN_CODE_ATTEMPTS = 10


class PackRequired(Exception):
    """study mode needs a pack_id; house mode must not have one."""


@dataclass(frozen=True)
class _Drawn:
    id: uuid.UUID
    difficulty: int
    correct_index: int
    stem: str
    options: list[str]


def _pool_filter(req: SessionGenerateRequest) -> ColumnElement[bool]:
    if req.mode == "study":
        if req.pack_id is None:
            raise PackRequired()
        where = Question.pack_id == req.pack_id
    else:
        if req.pack_id is not None:
            raise PackRequired()
        # House pool: live, and never a pack question (spec §8 insists on
        # this one; tests/test_sessions.py proves it).
        where = and_(Question.status == "live", Question.pack_id.is_(None))
    where = and_(where, Question.region.in_((req.region, "global")))
    if req.category_ids:
        where = and_(where, Question.category_id.in_(req.category_ids))
    return where


def _pool_query(req: SessionGenerateRequest):
    return (
        select(
            Question.id,
            Question.difficulty,
            Question.correct_index,
            QuestionTranslation.stem,
            QuestionTranslation.options,
        )
        .join(
            QuestionTranslation,
            and_(
                QuestionTranslation.question_id == Question.id,
                QuestionTranslation.locale == req.locale,
            ),
        )
        .where(_pool_filter(req))
    )


async def _draw(
    db: AsyncSession,
    req: SessionGenerateRequest,
    limit: int,
    *,
    difficulty: tuple[int, int] | None = None,
    exclude: Collection[uuid.UUID] = (),
) -> list[_Drawn]:
    if limit <= 0:
        return []
    query = _pool_query(req)
    if difficulty is not None:
        query = query.where(Question.difficulty.between(*difficulty))
    if exclude:
        query = query.where(Question.id.not_in(list(exclude)))
    rows = await db.execute(query.order_by(func.random()).limit(limit))
    return [_Drawn(*row) for row in rows]


def ramp_sizes(count: int) -> list[int]:
    """Band sizes for a ramp of `count` questions; the middle band absorbs
    the rounding so they always sum to `count`."""
    low = round(count * RAMP_BANDS[0][1])
    high = round(count * RAMP_BANDS[2][1])
    return [low, count - low - high, high]


async def draw_questions(db: AsyncSession, req: SessionGenerateRequest) -> list[_Drawn]:
    """The drawn set in play order; shorter than requested only when the
    pool is exhausted. No question id appears twice (each draw excludes
    what the previous ones took)."""
    if req.difficulty_curve == "flat":
        return await _draw(db, req, req.question_count)

    drawn: list[_Drawn] = []
    for (band, _), size in zip(RAMP_BANDS, ramp_sizes(req.question_count)):
        drawn += await _draw(db, req, size, difficulty=band, exclude={q.id for q in drawn})
    # A thin band leaves a gap: fill it from whatever difficulty is left.
    missing = req.question_count - len(drawn)
    if missing:
        drawn += await _draw(db, req, missing, exclude={q.id for q in drawn})
    return sorted(drawn, key=lambda q: q.difficulty)  # stable: random within a band


def shuffle_options(rng: random.Random) -> list[int]:
    """A permutation p: shown option i is original option p[i]."""
    order = list(range(4))
    rng.shuffle(order)
    return order


def new_join_code() -> str:
    return "".join(secrets.choice(JOIN_CODE_ALPHABET) for _ in range(JOIN_CODE_LENGTH))


async def generate_session(
    db: AsyncSession, req: SessionGenerateRequest, host: User
) -> tuple[GameSession, SessionQuestionsCache, int | None]:
    """Create the session with its question set (flush, no commit).

    Returns the session, the server-side payload to cache, and `short_by`
    (None when the request was filled)."""
    drawn = await draw_questions(db, req)
    rng = random.Random()
    session = GameSession(
        host_id=host.id,
        locale=req.locale,
        region=req.region,
        mode=req.mode,
        pack_id=req.pack_id,
        category_ids=req.category_ids,
        question_count=req.question_count,
        status="lobby",  # Phase 2 owns lobby -> running (started_at is set then)
    )
    payload: list[SessionQuestionServer] = []
    for ordinal, q in enumerate(drawn):
        order = shuffle_options(rng)
        session.questions.append(
            SessionQuestion(ordinal=ordinal, question_id=q.id, option_order=order)
        )
        payload.append(
            SessionQuestionServer(
                id=q.id,
                stem=q.stem,
                options=[q.options[j] for j in order],
                correct_index=order.index(q.correct_index),
                ordinal=ordinal,
            )
        )
    await _insert_with_join_code(db, session)
    cache = SessionQuestionsCache(session_id=session.id, locale=req.locale, questions=payload)
    short_by = req.question_count - len(drawn)
    return session, cache, short_by or None


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
