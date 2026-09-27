"""Replaying a session through the pure engine (§6, §9: a disputed game
replayed from `session_events` gives the identical result).

Nothing but what the DB holds goes in: the resolved config and rng seed
on the session row, the draw in `session_questions` (`load_draw` puts
it back in the engine's shape, in the order the live game had it) and
the event log with its server-time stamps. The engine is deterministic
given those, so the state that comes out is the state the live game
had after its last logged event — in particular the final one, for a
finished game.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.game import engine as eng
from app.game import snapshot
from app.game.engine import GameState
from app.game.seed import engine_rng
from app.models import GameSession, SessionEvent
from app.services import sessions as svc


class ReplayError(Exception):
    pass


LoggedEvent = tuple[int, int, str, dict[str, Any]]  # seq, at_ms, kind, payload


async def load_events(db: AsyncSession, session: GameSession) -> list[LoggedEvent]:
    """The session's log in order, checked for gaps (a replay of half the
    events proves nothing)."""
    rows = (
        await db.execute(
            select(SessionEvent.seq, SessionEvent.at_ms, SessionEvent.kind, SessionEvent.payload)
            .where(SessionEvent.session_id == session.id)
            .order_by(SessionEvent.seq)
        )
    ).all()
    expected = 1
    for seq, *_ in rows:
        if seq != expected:
            raise ReplayError(f"session {session.join_code}: event log jumps from seq {expected - 1} to {seq}")
        expected += 1
    return [tuple(r) for r in rows]  # type: ignore[misc]


async def replay(db: AsyncSession, session_id: uuid.UUID) -> GameState:
    """The engine state after every logged event of the session, in
    order. Raises ReplayError for an unknown or never-started session,
    or a log with a gap in it."""
    session = await db.get(GameSession, session_id)
    if session is None:
        raise ReplayError(f"no session {session_id}")
    try:
        draw = await svc.load_draw(db, session)
    except svc.NotStarted:
        raise ReplayError(f"session {session.join_code} never started") from None
    rows = await load_events(db, session)
    state = eng.new_game(draw.config, draw.pools, draw.block_reserve)
    rng = engine_rng(draw.seed)
    for _, at_ms, kind, payload in rows:
        event = snapshot.decode_event(kind, payload)
        state, _ = eng.step(state, event, at_ms, rng, copy_state=False)
    return state
