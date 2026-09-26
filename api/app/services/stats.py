"""Incremental rollup of question_serves into question_stats (spec §3.1:
"updated by the worker in batches, not on every write").

The rollup keeps a high-water mark — the last question_serves.id already
folded in — in rollup_watermarks. A batch aggregates the serves in
(last_id, upper] grouped by question, adds them onto question_stats with one
INSERT ... ON CONFLICT, and moves the mark to `upper`. Nothing already
counted is ever read again.

`upper` is the newest serve that is at least `grace` old (by served_at),
capped so a batch holds at most `batch_size` rows. The grace covers the one way an id-based mark can
lose a row: a transaction that took id N but had not committed when a later
id was rolled up. A serve insert is a single short statement, so a few
seconds is plenty; the rows inside the range are then read as they are,
whatever their served_at.
"""
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import QuestionServe, RollupWatermark

SERVES_WATERMARK = "question_serves"
ROLLUP_GRACE = timedelta(seconds=5)

# `serves` is what the health ratios and the serves floor are measured
# against, so it leaves out 'absent' (shown, but the player was gone — says
# nothing about the question); those go to `absents`. avg_response_ms is
# over the serves that carried a response time (timed_serves).
_UPSERT_SQL = text(
    """
    INSERT INTO question_stats (
        question_id, serves, correct, incorrect, timeouts, absents,
        timed_serves, response_ms_total, avg_response_ms, last_served_at
    )
    SELECT
        question_id,
        count(*) FILTER (WHERE outcome <> 'absent'),
        count(*) FILTER (WHERE outcome = 'correct'),
        count(*) FILTER (WHERE outcome = 'incorrect'),
        count(*) FILTER (WHERE outcome = 'timeout'),
        count(*) FILTER (WHERE outcome = 'absent'),
        count(response_ms),
        coalesce(sum(response_ms), 0),
        round(avg(response_ms))::int,
        max(served_at)
    FROM question_serves
    WHERE id > :last_id AND id <= :upper
    GROUP BY question_id
    ON CONFLICT (question_id) DO UPDATE SET
        serves            = question_stats.serves + EXCLUDED.serves,
        correct           = question_stats.correct + EXCLUDED.correct,
        incorrect         = question_stats.incorrect + EXCLUDED.incorrect,
        timeouts          = question_stats.timeouts + EXCLUDED.timeouts,
        absents           = question_stats.absents + EXCLUDED.absents,
        timed_serves      = question_stats.timed_serves + EXCLUDED.timed_serves,
        response_ms_total = question_stats.response_ms_total + EXCLUDED.response_ms_total,
        avg_response_ms   = CASE
            WHEN question_stats.timed_serves + EXCLUDED.timed_serves = 0 THEN NULL
            ELSE round(
                (question_stats.response_ms_total + EXCLUDED.response_ms_total)::numeric
                / (question_stats.timed_serves + EXCLUDED.timed_serves)
            )::int
        END,
        last_served_at    = greatest(question_stats.last_served_at, EXCLUDED.last_served_at)
    """
)


async def rollup_batch(
    db: AsyncSession, *, batch_size: int | None = None, grace: timedelta = ROLLUP_GRACE
) -> tuple[int, int] | None:
    """Fold one batch of new serves into question_stats (flush, no commit;
    the caller commits so the mark and the counts move together).

    Returns (serves folded, new watermark), or None when nothing settled
    has arrived since the last run. The watermark row is locked for the
    transaction, so two rollups cannot count the same batch twice."""
    batch_size = batch_size or settings.stats_rollup_batch_size
    await db.execute(
        pg_insert(RollupWatermark).values(name=SERVES_WATERMARK).on_conflict_do_nothing()
    )
    last_id = await db.scalar(
        select(RollupWatermark.last_id)
        .where(RollupWatermark.name == SERVES_WATERMARK)
        .with_for_update()
    )
    settled = await _newest_settled_id(db, last_id, grace)
    if settled is None:
        return None
    # The batch_size-th id above the mark (ids have gaps: rolled-back
    # inserts, cascaded deletes), so a batch is bounded by rows, not ids.
    window = (
        select(QuestionServe.id)
        .where(QuestionServe.id > last_id)
        .order_by(QuestionServe.id)
        .limit(batch_size)
        .subquery()
    )
    batch_end = await db.scalar(select(func.max(window.c.id)))
    upper = min(settled, batch_end)

    await db.execute(_UPSERT_SQL, {"last_id": last_id, "upper": upper})
    folded = await db.scalar(
        select(func.count())
        .select_from(QuestionServe)
        .where(QuestionServe.id > last_id, QuestionServe.id <= upper)
    )
    watermark = await db.get(RollupWatermark, SERVES_WATERMARK)
    watermark.last_id = upper
    watermark.updated_at = func.now()
    await db.flush()
    return folded, upper


async def serves_watermark(db: AsyncSession) -> int:
    """The last question_serves.id folded into question_stats (0 if never)."""
    last_id = await db.scalar(
        select(RollupWatermark.last_id).where(RollupWatermark.name == SERVES_WATERMARK)
    )
    return last_id or 0


async def unrolled_serves_exist(db: AsyncSession, *, grace: timedelta = ROLLUP_GRACE) -> bool:
    """True if a settled serve above the watermark is waiting for a rollup."""
    return await _newest_settled_id(db, await serves_watermark(db), grace) is not None


async def _newest_settled_id(db: AsyncSession, last_id: int, grace: timedelta) -> int | None:
    return await db.scalar(
        select(func.max(QuestionServe.id)).where(
            QuestionServe.id > last_id, QuestionServe.served_at <= func.now() - grace
        )
    )
