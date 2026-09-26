"""The stats rollup cron job (spec §3.1). Runs every
settings.stats_rollup_interval_minutes and folds whatever question_serves
rows arrived since the last run into question_stats, one committed batch at
a time, until it catches up.
"""
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.stats import ROLLUP_GRACE, rollup_batch, unrolled_serves_exist

log = logging.getLogger(__name__)

# A run stops after this many batches even if not caught up; the next cron
# tick continues from the watermark. Bounds one run's length behind a
# sudden backlog (a long outage, a backfill).
MAX_BATCHES_PER_RUN = 100


@dataclass
class RollupResult:
    batches: int = 0
    serves: int = 0
    last_id: int | None = None  # watermark after the run; None if nothing ran
    caught_up: bool = True


async def rollup_question_stats(ctx: dict[str, Any]) -> dict[str, Any]:
    """arq cron entry point; `ctx["session_factory"]` is set on startup."""
    async with ctx["session_factory"]() as db:
        result = await run_rollup(db)
    log.info("stats rollup: %s", result)
    return result.__dict__


async def run_rollup(
    db: AsyncSession,
    *,
    batch_size: int | None = None,
    grace: timedelta = ROLLUP_GRACE,
    max_batches: int = MAX_BATCHES_PER_RUN,
) -> RollupResult:
    """Batch after batch, each committed on its own, until nothing settled
    remains or max_batches is hit."""
    result = RollupResult()
    for _ in range(max_batches):
        batch = await rollup_batch(db, batch_size=batch_size, grace=grace)
        if batch is None:
            await db.commit()  # releases the watermark lock (and keeps a fresh row)
            return result
        folded, last_id = batch
        await db.commit()
        result.batches += 1
        result.serves += folded
        result.last_id = last_id
    result.caught_up = not await unrolled_serves_exist(db, grace=grace)
    return result
