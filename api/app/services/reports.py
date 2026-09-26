"""Player reports (spec §4): POST /questions/{id}/report.

A report is written straight to question_reports and counted straight onto
question_stats.reports — no rollup in between, so the suspect health view
(reports > 3) sees it on the next request. The stats row is created if the
question has never been served. One report per user per question, enforced
by question_reports_one_per_user_idx (migration 0004).
"""
import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import constraint_name
from app.models import Question, QuestionReport, QuestionStats, User
from app.schemas.telemetry import QuestionReportCreate

ONE_PER_USER_INDEX = "question_reports_one_per_user_idx"


class QuestionNotFound(Exception):
    pass


class AlreadyReported(Exception):
    """This user already reported this question (§4 rate limit)."""


async def report_question(
    db: AsyncSession, question_id: uuid.UUID, reporter: User, payload: QuestionReportCreate
) -> QuestionReport:
    """Write the report and bump the counter (flush, no commit)."""
    if await db.get(Question, question_id) is None:
        raise QuestionNotFound()
    report = QuestionReport(
        question_id=question_id, user_id=reporter.id, **payload.model_dump()
    )
    try:
        async with db.begin_nested():
            db.add(report)
            await db.flush()
    except IntegrityError as exc:
        if constraint_name(exc) == ONE_PER_USER_INDEX:
            raise AlreadyReported() from exc
        raise
    await db.execute(
        pg_insert(QuestionStats)
        .values(question_id=question_id, reports=1)
        .on_conflict_do_update(
            index_elements=[QuestionStats.question_id],
            set_={"reports": QuestionStats.reports + 1},
        )
    )
    return report
