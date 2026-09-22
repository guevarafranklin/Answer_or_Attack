"""Serves, stats rollup, and player reports (spec §4 Health views, Player reports)."""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel, ReportReason, ServeOutcome


class QuestionServeCreate(BaseModel):
    question_id: uuid.UUID
    session_id: uuid.UUID
    locale: str
    outcome: ServeOutcome
    response_ms: int | None = Field(default=None, ge=0)


class QuestionStatsRead(ORMModel):
    question_id: uuid.UUID
    serves: int
    correct: int
    incorrect: int
    timeouts: int
    reports: int
    avg_response_ms: int | None
    last_served_at: datetime | None


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
