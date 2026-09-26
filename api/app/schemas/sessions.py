"""Sessions (spec §4 Session content). Tables exist now; the engine is Phase 2.

`session_players.starting_xp` is secret until the reveal (spec §3.1) and is
guarded here, in the serializer: no schema in this module declares it, and
`SessionPlayerRead` forbids it explicitly. tests/test_schemas.py enforces this.
"""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Locale, ORMModel, Region, SessionMode, SessionStatus

DifficultyCurve = Literal["ramp", "flat"]


class SessionGenerateRequest(BaseModel):
    """POST /sessions/generate"""

    category_ids: list[uuid.UUID] = Field(default_factory=list)
    locale: Locale
    region: Region = "global"
    question_count: int = Field(default=15, ge=1, le=50)
    difficulty_curve: DifficultyCurve = "ramp"
    mode: SessionMode = "house"
    pack_id: uuid.UUID | None = None


class SessionQuestionOut(BaseModel):
    """One drawn question as the client sees it: options already shuffled
    for this session, and no `correct_index` — the server scores answers."""

    id: uuid.UUID
    stem: str
    options: list[str] = Field(min_length=4, max_length=4)
    ordinal: int


class SessionQuestionServer(SessionQuestionOut):
    """The server's copy of the same (Redis `session:{id}:questions`):
    `correct_index` is the position in the *shuffled* options."""

    correct_index: int = Field(ge=0, le=3)


class SessionQuestionsCache(BaseModel):
    """Payload cached at session:{id}:questions for 2 hours (spec §4), so
    Phase 2 scores a round without a DB query."""

    session_id: uuid.UUID
    locale: Locale
    questions: list[SessionQuestionServer]


class SessionGenerateResponse(BaseModel):
    session_id: uuid.UUID
    questions: list[SessionQuestionOut]
    # Set when the pool was too small to fill the request (spec §4).
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
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    players: list[SessionPlayerRead] = Field(default_factory=list)
