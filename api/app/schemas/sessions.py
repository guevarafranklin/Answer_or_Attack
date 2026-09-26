"""Sessions (Phase 2 spec §4, §6): the lobby is created over HTTP and the
question set is drawn at `start` into per-category pools plus a block
reserve.

`session_players.starting_xp` is secret until the reveal (spec §3.1) and is
guarded here, in the serializer: no schema in this module declares it, and
`SessionPlayerRead` forbids it explicitly. tests/test_schemas.py enforces this.
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Locale, ORMModel, Region, SessionMode, SessionStatus


class SessionCreateRequest(BaseModel):
    """POST /sessions — a lobby, no questions yet (Phase 2 §4).

    `config_overrides` is any subset of GameConfig fields (question_count
    included); the service validates it through GameConfig.from_overrides,
    so an unknown key or a bad value is a 422."""

    category_ids: list[uuid.UUID] = Field(default_factory=list)
    locale: Locale
    region: Region = "global"
    mode: SessionMode = "house"
    pack_id: uuid.UUID | None = None
    config_overrides: dict[str, Any] = Field(default_factory=dict)


class SessionCreateResponse(BaseModel):
    session_id: uuid.UUID
    join_code: str


class JoinRequest(BaseModel):
    """POST /sessions/{join_code}/join. `player_token` is only needed to
    rejoin a running game (app.services.players)."""

    display_name: str = Field(min_length=1, max_length=40)
    player_token: str | None = Field(default=None, max_length=128)


class JoinResponse(BaseModel):
    """`player_id` is the id used in every protocol message; `player_token`
    opens the WebSocket (`/ws/sessions/{join_code}?token=`) and is the
    rejoin credential — shown once, never stored in clear."""

    player_id: str
    player_token: str


class SessionQuestionOut(BaseModel):
    """One drawn question as the client sees it: options already shuffled
    for this session, and no `correct_index` — the server scores answers."""

    id: uuid.UUID
    stem: str
    options: list[str] = Field(min_length=4, max_length=4)
    ordinal: int


class SessionQuestionServer(SessionQuestionOut):
    """The server's copy of the same (Redis `session:{id}:questions`):
    `correct_index` is the position in the *shuffled* options, `pool` is
    the category id or 'block' (§6), `difficulty` feeds the engine's
    reserve fallback."""

    correct_index: int = Field(ge=0, le=3)
    pool: str
    difficulty: int = Field(ge=1, le=5)


class SessionQuestionsCache(BaseModel):
    """Payload cached at session:{id}:questions for 2 hours (spec §4), so
    Phase 2 scores a round without a DB query. `short_by` is the pool
    shortfall the draw reported (None when every pool was filled)."""

    session_id: uuid.UUID
    locale: Locale
    questions: list[SessionQuestionServer]
    short_by: int | None = None


class SessionPlayerRead(ORMModel):
    """Public view of a session player. `starting_xp` is intentionally absent
    and rejected if supplied, so it cannot leak before the reveal."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    session_id: uuid.UUID
    user_id: uuid.UUID
    display_name: str
    final_xp: int | None
    delta_xp: int | None
    joined_at: datetime


class SessionRead(ORMModel):
    """Public view of a session. `rng_seed` is intentionally absent and
    rejected if supplied: with the seed a client could predict the board,
    the auto-picks and everyone's starting XP."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: uuid.UUID
    host_id: uuid.UUID | None
    join_code: str
    locale: Locale
    region: str
    mode: SessionMode
    pack_id: uuid.UUID | None
    category_ids: list[uuid.UUID]
    question_count: int
    status: SessionStatus
    config_overrides: dict[str, Any]
    resolved_config: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    players: list[SessionPlayerRead] = Field(default_factory=list)
