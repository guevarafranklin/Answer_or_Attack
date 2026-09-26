"""question_serves ingestion (spec §3): one row per question shown to one
player. The game engine calls `record_serves` once per round in Phase 2;
there is no public endpoint.

This is the hot path (~15 rows per player per session), so a batch is one
executemany INSERT with no ORM objects, and nothing here touches
question_stats — the rollup (app.services.stats) does that in batches.
"""
from collections.abc import Sequence

from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import constraint_name
from app.models import QuestionServe
from app.schemas.telemetry import QuestionServeCreate


class UnknownQuestion(Exception):
    """A serve names a questions.id that does not exist."""


async def record_serves(db: AsyncSession, serves: Sequence[QuestionServeCreate]) -> int:
    """Insert the batch (flush, no commit). Returns the number of rows
    written; the whole batch is refused if any serve names an unknown
    question, so the caller's transaction stays usable."""
    if not serves:
        return 0
    rows = [serve.model_dump() for serve in serves]
    try:
        async with db.begin_nested():
            await db.execute(insert(QuestionServe), rows)
    except IntegrityError as exc:
        if constraint_name(exc) == "question_serves_question_id_fkey":
            raise UnknownQuestion("serve references a question that does not exist") from exc
        raise
    return len(rows)
