"""Generation jobs (spec §4 Generation, §5.1 job params)."""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import (
    GenerationKind,
    GenerationStatus,
    GradeBand,
    Locale,
    ORMModel,
    Region,
)


class GenerationParams(BaseModel):
    """Structured `generation_jobs.params` (§5.1). The API parses the admin's
    free text into this *before* queueing; free text never reaches the
    generator unparsed."""

    category_slug: str
    count: int = Field(ge=1, le=500)
    difficulty_min: int = Field(default=1, ge=1, le=5)
    difficulty_max: int = Field(default=5, ge=1, le=5)
    grade_bands: list[GradeBand] = Field(default_factory=list)
    region: Region = "global"
    locales: list[Locale] = Field(default_factory=lambda: ["en", "es"], min_length=1)
    style_notes: str | None = None

    @model_validator(mode="after")
    def _difficulty_range(self) -> "GenerationParams":
        if self.difficulty_min > self.difficulty_max:
            raise ValueError("difficulty_min must be <= difficulty_max")
        return self


class GenerationRequest(BaseModel):
    """POST /admin/generate. `prompt` is the admin's natural-language
    request; `params` is the parsed form the admin confirmed."""

    prompt: str = Field(min_length=1)
    params: GenerationParams
    kind: GenerationKind = "category"


class GenerationAccepted(BaseModel):
    """202 response of POST /admin/generate."""

    job_id: uuid.UUID


class GenerationJobRead(ORMModel):
    """GET /admin/generate/{job_id}: status + counts."""

    id: uuid.UUID
    kind: GenerationKind
    requested_by: uuid.UUID | None
    prompt: str | None
    params: GenerationParams
    status: GenerationStatus
    model: str | None
    requested_count: int | None
    produced_count: int
    accepted_count: int
    rejected_count: int
    cost_cents: int | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
