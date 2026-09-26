"""generation: generation_jobs (spec §3, §5)."""
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._common import (
    GENERATION_KINDS,
    GENERATION_STATUSES,
    Timestamp,
    TimestampNow,
    UuidPk,
    one_of,
)
from app.models.users import User


class GenerationJob(Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        one_of("kind", GENERATION_KINDS, name="generation_jobs_kind_check"),
        one_of("status", GENERATION_STATUSES, name="generation_jobs_status_check"),
    )

    id: Mapped[UuidPk]
    kind: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    prompt: Mapped[str | None] = mapped_column(Text)  # the admin's natural-language request
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)  # see spec §5.1
    status: Mapped[str] = mapped_column(Text, server_default="queued")
    model: Mapped[str | None] = mapped_column(Text)
    requested_count: Mapped[int | None] = mapped_column(Integer)
    # returned by model
    produced_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # survived validation, written as pending
    accepted_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # failed validation or deduped
    rejected_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_cents: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)  # human-readable summary
    # Structured diagnostics (migration 0002): rejection reasons with counts,
    # repeat_rate, chunk failures. See app.schemas.generation.GenerationStats.
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[TimestampNow]
    started_at: Mapped[Timestamp]
    finished_at: Mapped[Timestamp]

    requester: Mapped[User | None] = relationship()


Index("generation_jobs_status_created_at_idx", GenerationJob.status, GenerationJob.created_at.desc())
