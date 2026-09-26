"""sessions: sessions, session_players, session_questions (spec §3).

Tables now, engine in Phase 2. The class is `GameSession` so it never gets
confused with a SQLAlchemy `Session`.
"""
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    BigInteger,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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
    # GameConfig fields the host overrode (validated by from_overrides);
    # `{}` means defaults. Never contains the resolved starting XP.
    config_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    # The full GameConfig the game ran with, written at start once the
    # player-count tier has fixed starting_xp_choices. NULL in lobby.
    resolved_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Set at start; the draw's option shuffle and the engine rng derive
    # from it (app.game.seed), so a replay needs only seed + resolved
    # config + event log.
    rng_seed: Mapped[int | None] = mapped_column(BigInteger)
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


BLOCK_POOL = "block"


class SessionQuestion(Base):
    """Question set is drawn once at session start, never mid-round. Rows
    belong to a pool (Phase 2 §6): the category id as text for a category
    pool, or BLOCK_POOL for the block reserve; `ordinal` is unique per
    session and orders the rows inside their pool (migration 0005)."""

    __tablename__ = "session_questions"
    __table_args__ = (
        CheckConstraint(
            "pool = 'block' OR pool ~ '^[0-9a-f-]{36}$'", name="session_questions_pool_check"
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    question_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("questions.id"))
    pool: Mapped[str] = mapped_column(Text)
    # Per-session shuffle: shown option i is the question's option
    # option_order[i], so an answer i is correct iff
    # option_order[i] == question.correct_index (migration 0004).
    option_order: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), server_default=text("'{0,1,2,3}'")
    )

    session: Mapped[GameSession] = relationship(back_populates="questions")
    question: Mapped[Question] = relationship()


Index(
    "session_questions_pool_idx",
    SessionQuestion.session_id,
    SessionQuestion.pool,
    SessionQuestion.ordinal,
)
