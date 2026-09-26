"""Serves, stats rollup, health views and player reports (spec §3, §4)."""
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import Locale, ORMModel, QuestionStatus, ReportReason, ServeOutcome
from app.schemas.content import QuestionRead


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class QuestionServeCreate(BaseModel):
    """One question shown to one player (app.services.serves.record_serves).
    The game engine (Phase 2) builds these; nothing is exposed over HTTP."""

    question_id: uuid.UUID
    session_id: uuid.UUID
    user_id: uuid.UUID | None = None
    locale: Locale
    outcome: ServeOutcome
    response_ms: int | None = Field(default=None, ge=0)
    # Defaults to "now" here rather than in the DB so a batch can be written
    # in one executemany; backfills and the synthetic script set it.
    served_at: datetime = Field(default_factory=_utcnow)


class QuestionStatsRead(ORMModel):
    question_id: uuid.UUID
    serves: int
    correct: int
    incorrect: int
    timeouts: int
    absents: int
    reports: int
    avg_response_ms: int | None
    last_served_at: datetime | None


# ---------- health views (§4) ----------

HealthView = Literal["easy", "suspect", "dead"]


class HealthRow(QuestionStatsRead):
    """A question_stats row plus the ratio the view ranks on (correct/serves
    for easy and suspect, timeouts/serves for dead) and the question itself."""

    ratio: float
    question: QuestionRead


class HealthPage(BaseModel):
    view: HealthView
    items: list[HealthRow]
    page: int
    page_size: int
    total: int


class HealthCounts(BaseModel):
    easy: int
    suspect: int
    dead: int


class CategoryCounts(BaseModel):
    slug: str
    counts: dict[QuestionStatus, int]


class HealthSummary(BaseModel):
    """GET /admin/health/summary — the dashboard numbers (§6): house
    questions by status, by category × status and by locale × status, the
    pending backlog, and how many questions each health view holds."""

    pending_backlog: int
    by_status: dict[QuestionStatus, int]
    by_category: list[CategoryCounts]
    by_locale: dict[Locale, dict[QuestionStatus, int]]
    health: HealthCounts


# ---------- player reports ----------


class QuestionReportCreate(BaseModel):
    """POST /questions/{id}/report {reason, note?, session_id?}"""

    reason: ReportReason
    note: str | None = Field(default=None, max_length=1000)
    session_id: uuid.UUID | None = None


class QuestionReportRead(ORMModel):
    id: uuid.UUID
    question_id: uuid.UUID
    user_id: uuid.UUID | None
    session_id: uuid.UUID | None
    reason: ReportReason
    note: str | None
    resolved: bool
    created_at: datetime
