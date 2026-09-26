"""sessions: sessions, session_players, session_questions (spec §3).

Tables now, engine in Phase 2. The class is `GameSession` so it never gets
confused with a SQLAlchemy `Session`.
"""
import uuid

from sqlalchemy import ForeignKey, Integer, SmallInteger, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._common import (
    LOCALES,
    SESSION_MODES,
    SESSION_STATUSES,
    Timestamp,
    TimestampNow,
    UuidPk,
    one_of,
)
from app.models.content import Question


class GameSession(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("join_code", name="sessions_join_code_key"),
        one_of("locale", LOCALES, name="sessions_locale_check"),
        one_of("mode", SESSION_MODES, name="sessions_mode_check"),
        one_of("status", SESSION_STATUSES, name="sessions_status_check"),
    )

    id: Mapped[UuidPk]
    host_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    join_code: Mapped[str] = mapped_column(Text)
    locale: Mapped[str] = mapped_column(Text)
    region: Mapped[str] = mapped_column(Text, server_default="global")
    mode: Mapped[str] = mapped_column(Text)
    pack_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("study_packs.id", ondelete="SET NULL")
    )
    category_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), server_default=text("'{}'")
    )
    question_count: Mapped[int] = mapped_column(Integer, server_default=text("15"))
    status: Mapped[str] = mapped_column(Text, server_default="lobby")
    created_at: Mapped[TimestampNow]
    started_at: Mapped[Timestamp]
    ended_at: Mapped[Timestamp]

    players: Mapped[list["SessionPlayer"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    questions: Mapped[list["SessionQuestion"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="SessionQuestion.ordinal",
    )


class SessionPlayer(Base):
    __tablename__ = "session_players"

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    display_name: Mapped[str] = mapped_column(Text)
    # 10 | 18 | 30, secret until reveal. Never serialized to clients before
    # the reveal: app.schemas.sessions deliberately has no such field (§3.1).
    starting_xp: Mapped[int] = mapped_column(SmallInteger)
    final_xp: Mapped[int | None] = mapped_column(SmallInteger)
    delta_xp: Mapped[int | None] = mapped_column(SmallInteger)
    joined_at: Mapped[TimestampNow]

    session: Mapped[GameSession] = relationship(back_populates="players")


class SessionQuestion(Base):
    """Question set is drawn once at session start, never mid-round."""

    __tablename__ = "session_questions"

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    question_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("questions.id"))
    # Per-session shuffle: shown option i is the question's option
    # option_order[i], so an answer i is correct iff
    # option_order[i] == question.correct_index (migration 0004).
    option_order: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), server_default=text("'{0,1,2,3}'")
    )

    session: Mapped[GameSession] = relationship(back_populates="questions")
    question: Mapped[Question] = relationship()
