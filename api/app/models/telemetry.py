"""telemetry: question_serves, question_stats, question_reports (spec §3),
plus the rollup's high-water mark (migration 0003)."""
import uuid

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, Text, Uuid, false, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._common import (
    REPORT_REASONS,
    SERVE_OUTCOMES,
    Timestamp,
    TimestampNow,
    UuidPk,
    one_of,
)
from app.models.content import Question


class QuestionServe(Base):
    """One row per question shown to one player. High volume: ~15 rows per
    player per session. Partition by month once this passes ~50M rows."""

    __tablename__ = "question_serves"
    __table_args__ = (one_of("outcome", SERVE_OUTCOMES, name="question_serves_outcome_check"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("questions.id", ondelete="CASCADE")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    locale: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    response_ms: Mapped[int | None] = mapped_column(Integer)
    served_at: Mapped[TimestampNow]


Index("question_serves_question_id_served_at_idx", QuestionServe.question_id, QuestionServe.served_at.desc())
Index("question_serves_session_id_idx", QuestionServe.session_id)


class QuestionStats(Base):
    """Rollup so the admin dashboard never aggregates the raw table.
    Updated by the worker in batches from question_serves (spec §3.1)."""

    __tablename__ = "question_stats"

    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True
    )
    # serves = correct + incorrect + timeouts. An 'absent' serve (player gone)
    # is counted apart so it never dilutes a ratio or reaches the serves floor.
    serves: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    correct: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    incorrect: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    timeouts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    absents: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    reports: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    avg_response_ms: Mapped[int | None] = mapped_column(Integer)
    last_served_at: Mapped[Timestamp]
    # Running sum / count behind avg_response_ms, so the rollup can advance
    # the average without re-reading old serves (migration 0003).
    timed_serves: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    response_ms_total: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))

    question: Mapped[Question] = relationship()


class RollupWatermark(Base):
    """High-water mark of an incremental rollup: the last source-row id
    already folded in. One row per rollup (see app.services.stats)."""

    __tablename__ = "rollup_watermarks"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    last_id: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    updated_at: Mapped[TimestampNow]


class QuestionReport(Base):
    __tablename__ = "question_reports"
    __table_args__ = (one_of("reason", REPORT_REASONS, name="question_reports_reason_check"),)

    id: Mapped[UuidPk]
    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("questions.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    reason: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_at: Mapped[TimestampNow]

    question: Mapped[Question] = relationship()


Index(
    "question_reports_question_id_idx",
    QuestionReport.question_id,
    postgresql_where=QuestionReport.resolved == false(),
)
# §4: one report per user per question, enforced by the DB (migration 0004).
Index(
    "question_reports_one_per_user_idx",
    QuestionReport.question_id,
    QuestionReport.user_id,
    unique=True,
    postgresql_where=QuestionReport.user_id.is_not(None),
)
