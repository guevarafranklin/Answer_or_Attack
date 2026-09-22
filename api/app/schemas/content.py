"""Categories, study packs, questions and translations (spec §4)."""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import (
    GradeBand,
    Locale,
    ORMModel,
    QuestionSource,
    QuestionStatus,
    Region,
    StudyPackStatus,
)

# Spec §5.2: a stem longer than 120 chars is unreadable in 10 seconds, and
# an option longer than 60 chars. Hard product constraints, not style notes.
MAX_STEM_LEN = 120
MAX_OPTION_LEN = 60
OPTION_COUNT = 4

BulkAction = Literal["approve", "reject", "archive"]

# ---------- categories ----------


class CategoryTranslationIn(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None


class CategoryTranslationRead(ORMModel):
    locale: Locale
    name: str
    description: str | None


class CategoryCreate(BaseModel):
    """POST /admin/categories {slug, icon, translations:{en:{...}, es:{...}}}"""

    slug: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    icon: str | None = None
    sort_order: int = 0
    translations: dict[Locale, CategoryTranslationIn]


class CategoryUpdate(BaseModel):
    """PATCH /admin/categories/{id}; every field optional."""

    icon: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    translations: dict[Locale, CategoryTranslationIn] | None = None


class CategoryRead(ORMModel):
    id: uuid.UUID
    slug: str
    icon: str | None
    sort_order: int
    is_active: bool
    created_at: datetime
    translations: list[CategoryTranslationRead]


# ---------- study packs ----------


class StudyPackRead(ORMModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    source_filename: str | None
    page_count: int | None
    locale: Locale
    status: StudyPackStatus
    question_count: int
    share_code: str | None
    created_at: datetime


# ---------- questions ----------


def _validate_options(options: list[str]) -> list[str]:
    if len(options) != OPTION_COUNT:
        raise ValueError(f"exactly {OPTION_COUNT} options required")
    if any(len(o) > MAX_OPTION_LEN for o in options):
        raise ValueError(f"options must be at most {MAX_OPTION_LEN} characters")
    if len({o.strip().lower() for o in options}) != OPTION_COUNT:
        raise ValueError("options must be distinct")
    return options


class QuestionTranslationIn(BaseModel):
    stem: str = Field(min_length=1, max_length=MAX_STEM_LEN)
    options: list[str]
    explanation: str | None = None

    _check_options = field_validator("options")(_validate_options)


class QuestionTranslationRead(ORMModel):
    locale: Locale
    stem: str
    options: list[str]
    explanation: str | None


class QuestionRead(ORMModel):
    id: uuid.UUID
    category_id: uuid.UUID
    difficulty: int
    grade_band: GradeBand | None
    region: Region
    tags: list[str]
    correct_index: int
    status: QuestionStatus
    source: QuestionSource
    pack_id: uuid.UUID | None
    generation_job_id: uuid.UUID | None
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    translations: list[QuestionTranslationRead]


class QuestionUpdate(BaseModel):
    """PATCH /admin/questions/{id}: edit stem/options/correct_index/
    difficulty/region/tags. Text edits are per locale."""

    translations: dict[Locale, QuestionTranslationIn] | None = None
    correct_index: int | None = Field(default=None, ge=0, le=OPTION_COUNT - 1)
    difficulty: int | None = Field(default=None, ge=1, le=5)
    grade_band: GradeBand | None = None
    region: Region | None = None
    tags: list[str] | None = None


class QuestionListQuery(BaseModel):
    """GET /admin/questions?status=&category=&locale=&page="""

    status: QuestionStatus | None = None
    category: str | None = None  # slug
    locale: Locale | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class QuestionBulkAction(BaseModel):
    """POST /admin/questions/bulk {ids:[], action}"""

    ids: list[uuid.UUID] = Field(min_length=1)
    action: BulkAction
