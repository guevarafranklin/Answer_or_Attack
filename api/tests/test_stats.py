"""§9 step 7: question_serves ingestion and the incremental rollup into
question_stats (spec §3.1)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Category, Question, QuestionServe, QuestionStats, QuestionTranslation
from app.schemas.telemetry import QuestionServeCreate
from app.services.health import HEALTH_MIN_SERVES, health_counts
from app.services.serves import UnknownQuestion, record_serves
from app.services.stats import (
    SERVES_WATERMARK,
    rollup_batch,
    serves_watermark,
    unrolled_serves_exist,
)
from app.services.validation import content_hash
from app.workers.stats import run_rollup

NO_GRACE = timedelta(0)
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


async def _question(db: AsyncSession, i: int = 1, *, category: Category | None = None) -> Question:
    if category is None:
        category = Category(slug=f"cat-{uuid.uuid4().hex[:8]}")
        db.add(category)
        await db.flush()
    q = Question(
        category_id=category.id,
        difficulty=2,
        correct_index=0,
        status="live",
        content_hash=content_hash(f"Q{i} {uuid.uuid4()}"),
        translations=[
            QuestionTranslation(locale="en", stem=f"Q{i}?", options=["a", "b", "c", "d"]),
            QuestionTranslation(locale="es", stem=f"¿P{i}?", options=["a", "b", "c", "d"]),
        ],
    )
    db.add(q)
    await db.flush()
    return q


def _serve(
    question: Question,
    outcome: str = "correct",
    *,
    response_ms: int | None = 3000,
    at: datetime = T0,
    session_id: uuid.UUID | None = None,
) -> QuestionServeCreate:
    return QuestionServeCreate(
        question_id=question.id,
        session_id=session_id or uuid.uuid4(),
        locale="en",
        outcome=outcome,
        response_ms=response_ms,
        served_at=at,
    )


async def _stats(db: AsyncSession, question: Question) -> QuestionStats | None:
    return await db.get(QuestionStats, question.id, populate_existing=True)


# ---------- ingestion ----------


@pytest.mark.asyncio
async def test_record_serves_writes_rows_and_nothing_else(db: AsyncSession):
    q = await _question(db)
    session = uuid.uuid4()
    n = await record_serves(
        db,
        [
            _serve(q, "correct", response_ms=2500, session_id=session),
            _serve(q, "timeout", response_ms=None, session_id=session),
        ],
    )
    assert n == 2

    rows = (await db.execute(select(QuestionServe).order_by(QuestionServe.id))).scalars().all()
    assert [(r.question_id, r.session_id, r.outcome, r.response_ms) for r in rows] == [
        (q.id, session, "correct", 2500),
        (q.id, session, "timeout", None),
    ]
    assert rows[0].served_at == T0
    # A serve is the hot path: it never touches the rollup.
    assert await _stats(db, q) is None


@pytest.mark.asyncio
async def test_record_serves_defaults_served_at_to_now(db: AsyncSession):
    q = await _question(db)
    before = datetime.now(timezone.utc)
    serve = QuestionServeCreate(
        question_id=q.id, session_id=uuid.uuid4(), locale="es", outcome="absent"
    )
    assert serve.served_at >= before
    await record_serves(db, [serve])


@pytest.mark.asyncio
async def test_record_serves_empty_batch(db: AsyncSession):
    assert await record_serves(db, []) == 0


@pytest.mark.asyncio
async def test_record_serves_refuses_unknown_question_and_keeps_session_usable(
    db: AsyncSession,
):
    q = await _question(db)
    ghost = Question(id=uuid.uuid4())  # never added
    with pytest.raises(UnknownQuestion):
        await record_serves(db, [_serve(q), _serve(ghost)])
    # Whole batch refused, transaction still fine.
    assert await db.scalar(select(func.count()).select_from(QuestionServe)) == 0
    assert await record_serves(db, [_serve(q)]) == 1


def test_serve_schema_validates_outcome_and_response():
    base = dict(question_id=uuid.uuid4(), session_id=uuid.uuid4())
    with pytest.raises(ValueError):
        QuestionServeCreate(**base, locale="en", outcome="skipped")
    with pytest.raises(ValueError):
        QuestionServeCreate(**base, locale="en", outcome="correct", response_ms=-1)
    with pytest.raises(ValueError):
        QuestionServeCreate(**base, locale="fr", outcome="correct")


# ---------- rollup ----------


@pytest.mark.asyncio
async def test_rollup_nothing_to_do(db: AsyncSession):
    assert await rollup_batch(db, grace=NO_GRACE) is None
    assert await serves_watermark(db) == 0
    assert not await unrolled_serves_exist(db, grace=NO_GRACE)


@pytest.mark.asyncio
async def test_rollup_aggregates_per_question(db: AsyncSession):
    a, b = await _question(db, 1), await _question(db, 2)
    await record_serves(
        db,
        [
            _serve(a, "correct", response_ms=2000, at=T0),
            _serve(a, "correct", response_ms=4000, at=T0 + timedelta(minutes=1)),
            _serve(a, "incorrect", response_ms=3000, at=T0 + timedelta(minutes=2)),
            _serve(a, "timeout", response_ms=None, at=T0 + timedelta(minutes=3)),
            _serve(a, "absent", response_ms=None, at=T0 - timedelta(days=1)),
            _serve(b, "incorrect", response_ms=1000, at=T0),
        ],
    )
    assert await unrolled_serves_exist(db, grace=NO_GRACE)

    folded, last_id = await rollup_batch(db, grace=NO_GRACE)
    assert folded == 6
    assert last_id == await db.scalar(select(func.max(QuestionServe.id)))
    assert await serves_watermark(db) == last_id

    sa = await _stats(db, a)
    assert (sa.serves, sa.correct, sa.incorrect, sa.timeouts) == (4, 2, 1, 1)
    assert (sa.absents, sa.reports) == (1, 0)
    assert (sa.timed_serves, sa.response_ms_total, sa.avg_response_ms) == (3, 9000, 3000)
    assert sa.last_served_at == T0 + timedelta(minutes=3)
    sb = await _stats(db, b)
    assert (sb.serves, sb.correct, sb.incorrect, sb.timeouts) == (1, 0, 1, 0)
    assert (sb.timed_serves, sb.avg_response_ms) == (1, 1000)

    # Caught up: a second run reads nothing.
    assert await rollup_batch(db, grace=NO_GRACE) is None
    assert not await unrolled_serves_exist(db, grace=NO_GRACE)


@pytest.mark.asyncio
async def test_rollup_is_incremental_from_the_watermark(db: AsyncSession):
    """New serves are added onto the existing counts; the old ones are never
    re-read (proved by the watermark and the running average)."""
    q = await _question(db)
    await record_serves(db, [_serve(q, "correct", response_ms=1000, at=T0)])
    _, mark1 = await rollup_batch(db, grace=NO_GRACE)

    await record_serves(
        db,
        [
            _serve(q, "correct", response_ms=4000, at=T0 + timedelta(hours=1)),
            _serve(q, "incorrect", response_ms=None, at=T0 + timedelta(hours=2)),
        ],
    )
    folded, mark2 = await rollup_batch(db, grace=NO_GRACE)
    assert folded == 2  # only the new rows
    assert mark2 > mark1

    s = await _stats(db, q)
    assert (s.serves, s.correct, s.incorrect) == (3, 2, 1)
    assert (s.timed_serves, s.response_ms_total, s.avg_response_ms) == (2, 5000, 2500)
    assert s.last_served_at == T0 + timedelta(hours=2)


@pytest.mark.asyncio
async def test_rollup_avg_stays_null_without_response_times(db: AsyncSession):
    q = await _question(db)
    await record_serves(db, [_serve(q, "timeout", response_ms=None)] * 3)
    await rollup_batch(db, grace=NO_GRACE)
    s = await _stats(db, q)
    assert (s.serves, s.timed_serves, s.avg_response_ms) == (3, 0, None)


@pytest.mark.asyncio
async def test_absent_serves_count_apart(db: AsyncSession):
    """'absent' means the player was not there: it is tallied in `absents`
    and never in `serves`, so it moves neither the ratios nor the floor."""
    q = await _question(db)
    below = HEALTH_MIN_SERVES - 1
    await record_serves(
        db,
        [_serve(q, "correct", at=T0)] * below
        + [_serve(q, "absent", response_ms=None, at=T0 + timedelta(minutes=1))] * 20,
    )
    await rollup_batch(db, grace=NO_GRACE)
    s = await _stats(db, q)
    assert (s.serves, s.correct, s.absents) == (below, below, 20)
    assert s.serves == s.correct + s.incorrect + s.timeouts
    assert s.last_served_at == T0 + timedelta(minutes=1)  # it was shown, though
    assert (await health_counts(db)).easy == 0  # 49 real serves: under the floor

    await record_serves(db, [_serve(q, "correct", at=T0 + timedelta(minutes=2))])
    await rollup_batch(db, grace=NO_GRACE)
    s = await _stats(db, q)
    assert (s.serves, s.absents) == (HEALTH_MIN_SERVES, 20)
    assert (await health_counts(db)).easy == 1  # ratio 1.0, not 50/70


@pytest.mark.asyncio
async def test_rollup_keeps_reports_and_older_last_served(db: AsyncSession):
    """The rollup owns the serve counters only: `reports` (the report
    endpoint's) is left alone, and a backfilled old serve never moves
    last_served_at backwards."""
    q = await _question(db)
    db.add(QuestionStats(question_id=q.id, reports=7, last_served_at=T0 + timedelta(days=9)))
    await db.flush()
    await record_serves(db, [_serve(q, "correct", at=T0)])
    await rollup_batch(db, grace=NO_GRACE)
    s = await _stats(db, q)
    assert (s.serves, s.reports) == (1, 7)
    assert s.last_served_at == T0 + timedelta(days=9)


@pytest.mark.asyncio
async def test_rollup_batches_by_id_range(db: AsyncSession):
    q = await _question(db)
    await record_serves(db, [_serve(q, "correct", at=T0)] * 7)
    first_id = await db.scalar(select(func.min(QuestionServe.id)))

    folded, mark = await rollup_batch(db, batch_size=3, grace=NO_GRACE)
    assert folded == 3
    assert mark == first_id + 2
    assert (await _stats(db, q)).serves == 3

    folded, mark = await rollup_batch(db, batch_size=3, grace=NO_GRACE)
    assert (folded, mark) == (3, first_id + 5)
    folded, mark = await rollup_batch(db, batch_size=3, grace=NO_GRACE)
    assert (folded, mark) == (1, first_id + 6)
    assert (await _stats(db, q)).serves == 7
    assert await rollup_batch(db, batch_size=3, grace=NO_GRACE) is None


@pytest.mark.asyncio
async def test_rollup_batches_are_rows_not_ids(db: AsyncSession):
    """Serve ids have gaps (rolled-back inserts, deleted questions); a batch
    still holds batch_size rows."""
    q = await _question(db)
    await record_serves(db, [_serve(q, "correct", at=T0)] * 4)
    gone = await _question(db, 2)
    await record_serves(db, [_serve(gone, "correct", at=T0)] * 50)
    await db.delete(gone)  # cascades: ids now jump by 50
    await db.flush()
    await record_serves(db, [_serve(q, "correct", at=T0)] * 3)

    folded, _ = await rollup_batch(db, batch_size=5, grace=NO_GRACE)
    assert folded == 5
    folded, _ = await rollup_batch(db, batch_size=5, grace=NO_GRACE)
    assert folded == 2
    assert (await _stats(db, q)).serves == 7


@pytest.mark.asyncio
async def test_rollup_waits_for_the_grace_period(db: AsyncSession):
    """A serve younger than `grace` is not folded yet; its id caps `upper`
    so an in-flight insert with a lower id cannot be skipped over."""
    q = await _question(db)
    now = datetime.now(timezone.utc)
    await record_serves(db, [_serve(q, "correct", at=now - timedelta(minutes=1))])
    await record_serves(db, [_serve(q, "correct", at=now - timedelta(seconds=1))])

    folded, _ = await rollup_batch(db, grace=timedelta(seconds=30))
    assert folded == 1
    assert await unrolled_serves_exist(db, grace=NO_GRACE)
    assert not await unrolled_serves_exist(db, grace=timedelta(seconds=30))
    folded, _ = await rollup_batch(db, grace=NO_GRACE)
    assert folded == 1
    assert (await _stats(db, q)).serves == 2


@pytest.mark.asyncio
async def test_run_rollup_loops_until_caught_up(db: AsyncSession):
    q = await _question(db)
    await record_serves(db, [_serve(q, "timeout", response_ms=None)] * 10)

    result = await run_rollup(db, batch_size=4, grace=NO_GRACE)
    assert (result.batches, result.serves, result.caught_up) == (3, 10, True)
    assert result.last_id == await serves_watermark(db)
    assert (await _stats(db, q)).timeouts == 10

    again = await run_rollup(db, batch_size=4, grace=NO_GRACE)
    assert (again.batches, again.serves, again.last_id, again.caught_up) == (0, 0, None, True)


@pytest.mark.asyncio
async def test_run_rollup_stops_at_max_batches(db: AsyncSession):
    q = await _question(db)
    await record_serves(db, [_serve(q, "correct")] * 5)
    result = await run_rollup(db, batch_size=2, grace=NO_GRACE, max_batches=2)
    assert (result.batches, result.serves, result.caught_up) == (2, 4, False)
    # The next run picks up from the mark.
    result = await run_rollup(db, batch_size=2, grace=NO_GRACE)
    assert (result.batches, result.serves, result.caught_up) == (1, 1, True)
    assert (await _stats(db, q)).correct == 5


@pytest.mark.asyncio
async def test_watermark_row_is_named(db: AsyncSession):
    q = await _question(db)
    await record_serves(db, [_serve(q)])
    await rollup_batch(db, grace=NO_GRACE)
    from app.models import RollupWatermark

    mark = await db.get(RollupWatermark, SERVES_WATERMARK)
    assert mark is not None and mark.last_id == await serves_watermark(db)
