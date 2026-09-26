"""Categories, study packs, questions and translations (spec §4)."""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models._common import LOCALES
from app.rules import (
    OPTION_COUNT,
    RuleViolation,
    contains_forbidden_phrase,
    validate_options,
    validate_stem,
)
from app.schemas.common import (
    GradeBand,
    Locale,
    ORMModel,
    QuestionSource,
    QuestionStatus,
    Region,
    StudyPackStatus,
)

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
    """POST /admin/categories {slug, icon, translations:{en:{...}, es:{...}}}

    Both locales are required on create (spec §1: one row, two locales).
    """

    slug: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    icon: str | None = None
    sort_order: int = 0
    translations: dict[Locale, CategoryTranslationIn]

    @field_validator("translations")
    @classmethod
    def _all_locales_present(cls, value: dict) -> dict:
        missing = sorted(set(LOCALES) - set(value))
        if missing:
            raise ValueError(f"missing translation for locale(s): {', '.join(missing)}")
        return value


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


class QuestionTranslationIn(BaseModel):
    """Text for one locale. Limits come from app.rules so the generation
    validator (§5.2) and the admin edit path can never disagree."""

    stem: str
    options: list[str]
    explanation: str | None = None

    _check_stem = field_validator("stem")(validate_stem)
    _check_options = field_validator("options")(validate_options)

    @model_validator(mode="after")
    def _no_forbidden_phrase(self) -> "QuestionTranslationIn":
        if any(contains_forbidden_phrase(t) for t in (self.stem, *self.options)):
            raise RuleViolation(
                "forbidden_phrase", "stem and options must not say 'all/none of the above'"
            )
        return self


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
    locale: Locale | None = None  # only questions that have text in this locale
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class QuestionPage(BaseModel):
    items: list[QuestionRead]
    page: int
    page_size: int
    total: int


class QuestionBulkAction(BaseModel):
    """POST /admin/questions/bulk {ids:[], action}"""

    ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    action: BulkAction


class QuestionBulkFailure(BaseModel):
    id: uuid.UUID
    detail: str


class QuestionBulkResult(BaseModel):
    """Per-id outcome: the ids that changed and, for the rest, why not.
    A bulk call succeeds as a whole even if some ids fail."""

    updated: list[uuid.UUID]
    failed: list[QuestionBulkFailure]
