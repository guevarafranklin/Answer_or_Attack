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

MAX_JOB_COUNT = 200


class GenerationParams(BaseModel):
    """Structured `generation_jobs.params` (§5.1). The API parses the admin's
    free text into this *before* queueing; free text never reaches the
    generator unparsed."""

    category_slug: str
    count: int = Field(ge=1, le=MAX_JOB_COUNT)
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


class ParseRequest(BaseModel):
    """POST /admin/generate/parse: the admin's free text, nothing else."""

    prompt: str = Field(min_length=1, max_length=4000)


class ParseResponse(BaseModel):
    """What the admin confirms before a job is created. `notes` lists every
    adjustment the parser made that the admin should know about (a clamped
    count, a defaulted difficulty range)."""

    params: GenerationParams
    notes: list[str] = Field(default_factory=list)


class GenerationStats(BaseModel):
    """`generation_jobs.stats`, written by the worker when a job finishes.
    Every field defaults so a queued/running job (stats = {}) reads cleanly."""

    # reason code -> count, over every rejected item (an item with several
    # faults counts once per code).
    rejections: dict[str, int] = Field(default_factory=dict)
    # Diagnostic repeat tracking (app.services.validation.RepeatCounter).
    emitted: int = 0
    repeated: int = 0
    repeat_rate: float = 0.0
    chunks_total: int = 0
    chunks_failed: int = 0
    chunk_errors: list[str] = Field(default_factory=list)
    # Raw model usage behind cost_cents, so the cost can be re-derived when
    # prices change.
    input_tokens: int = 0
    output_tokens: int = 0


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
    stats: GenerationStats
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
