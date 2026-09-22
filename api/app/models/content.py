"""content: study_packs, categories, questions and their translations (spec §3).

Ground rules that live in this schema (spec §1):
- One question, two locales: `questions` holds language-independent facts,
  `question_translations` holds text. `correct_index` is on the question.
- House content has `pack_id IS NULL`. Every house-pool query must filter on
  that; the partial indexes below are keyed on it.
"""
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    and_,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._common import (
    GRADE_BANDS,
    LOCALES,
    QUESTION_SOURCES,
    QUESTION_STATUSES,
    REGIONS,
    STUDY_PACK_STATUSES,
    Timestamp,
    TimestampNow,
    UuidPk,
    one_of,
)
from app.models.users import User


class StudyPack(Base):
    __tablename__ = "study_packs"
    __table_args__ = (
        UniqueConstraint("share_code", name="study_packs_share_code_key"),
        one_of("locale", LOCALES, name="study_packs_locale_check"),
        one_of("status", STUDY_PACK_STATUSES, name="study_packs_status_check"),
    )

    id: Mapped[UuidPk]
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(Text)
    source_filename: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    locale: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="uploading")
    question_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    share_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[TimestampNow]

    owner: Mapped[User] = relationship()
    questions: Mapped[list["Question"]] = relationship(back_populates="pack")


Index("study_packs_owner_id_created_at_idx", StudyPack.owner_id, StudyPack.created_at.desc())


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("slug", name="categories_slug_key"),)

    id: Mapped[UuidPk]
    slug: Mapped[str] = mapped_column(Text)  # 'math','bible','cars','world-history'
    icon: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())
    created_at: Mapped[TimestampNow]

    translations: Mapped[list["CategoryTranslation"]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class CategoryTranslation(Base):
    __tablename__ = "category_translations"
    __table_args__ = (one_of("locale", LOCALES, name="category_translations_locale_check"),)

    category_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True
    )
    locale: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    category: Mapped[Category] = relationship(back_populates="translations")


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (
        CheckConstraint("difficulty BETWEEN 1 AND 5", name="questions_difficulty_check"),
        one_of("grade_band", GRADE_BANDS, name="questions_grade_band_check"),
        one_of("region", REGIONS, name="questions_region_check"),
        CheckConstraint("correct_index BETWEEN 0 AND 3", name="questions_correct_index_check"),
        one_of("status", QUESTION_STATUSES, name="questions_status_check"),
        one_of("source", QUESTION_SOURCES, name="questions_source_check"),
    )

    id: Mapped[UuidPk]
    category_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("categories.id"))
    difficulty: Mapped[int] = mapped_column(SmallInteger)
    grade_band: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str] = mapped_column(Text, server_default="global")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    correct_index: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    source: Mapped[str] = mapped_column(Text, server_default="ai")
    # NULL = house content
    pack_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("study_packs.id", ondelete="CASCADE")
    )
    generation_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("generation_jobs.id", ondelete="SET NULL")
    )
    # sha256 of normalized en stem; dedupe (spec §3.1)
    content_hash: Mapped[str] = mapped_column(Text)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    reviewed_at: Mapped[Timestamp]
    created_at: Mapped[TimestampNow]
    updated_at: Mapped[TimestampNow] = mapped_column(onupdate=func.now())

    category: Mapped[Category] = relationship()
    pack: Mapped[StudyPack | None] = relationship(back_populates="questions")
    translations: Mapped[list["QuestionTranslation"]] = relationship(
        back_populates="question", cascade="all, delete-orphan"
    )


# House content must be unique. Pack content may legitimately repeat
# across users' uploads, so the constraint is partial.
Index(
    "questions_house_hash_uniq",
    Question.content_hash,
    unique=True,
    postgresql_where=Question.pack_id.is_(None),
)
# The hot path: drawing a house question set.
Index(
    "questions_house_draw",
    Question.category_id,
    Question.difficulty,
    Question.region,
    postgresql_where=and_(Question.status == "live", Question.pack_id.is_(None)),
)
Index(
    "questions_review_queue",
    Question.status,
    Question.created_at.desc(),
    postgresql_where=Question.pack_id.is_(None),
)
Index("questions_pack_id_idx", Question.pack_id, postgresql_where=Question.pack_id.is_not(None))


class QuestionTranslation(Base):
    __tablename__ = "question_translations"
    __table_args__ = (
        one_of("locale", LOCALES, name="question_translations_locale_check"),
        CheckConstraint(
            "jsonb_array_length(options) = 4", name="question_translations_options_check"
        ),
    )

    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True
    )
    locale: Mapped[str] = mapped_column(Text, primary_key=True)
    stem: Mapped[str] = mapped_column(Text)
    options: Mapped[list[Any]] = mapped_column(JSONB)
    explanation: Mapped[str | None] = mapped_column(Text)

    question: Mapped[Question] = relationship(back_populates="translations")
