"""Seats and player tokens (spec §4, §2.8).

    POST /sessions/{join_code}/join {display_name} → {player_id, player_token}

The caller's user identity (app.auth.current_player) decides *who* joins;
the token decides *which socket* may speak for that seat. It is opaque
and random, stored only as a sha256 digest, and scoped to one session:
knowing a user id (public in every roster) gets nobody a socket, and
a leaked DB row cannot be replayed.

Lobby: a new seat, or a fresh token for a seat the same user already
holds (rejoining from another device before the game starts). Running:
only a rejoin — the same user presenting the seat's current token gets
its player id back; anyone else is refused (§2.8: late joiners do not
enter a running game). Finished/abandoned: refused.

Once the engine has the seat (first WebSocket connect), `player_id` in
the protocol is `str(user_id)`.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GameSession, SessionPlayer, User
from app.services.sessions import config_from

TOKEN_BYTES = 32


class NotJoinable(Exception):
    """The session is not accepting this join. `reason` is a short code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def player_id_of(user_id: uuid.UUID) -> str:
    return str(user_id)


@dataclass(frozen=True, slots=True)
class Seat:
    player: SessionPlayer
    token: str

    @property
    def player_id(self) -> str:
        return player_id_of(self.player.user_id)


async def session_by_join_code(db: AsyncSession, join_code: str) -> GameSession | None:
    return await db.scalar(select(GameSession).where(GameSession.join_code == join_code))


async def join(
    db: AsyncSession,
    session: GameSession,
    user: User,
    display_name: str,
    *,
    token: str | None = None,
) -> Seat:
    """Take (or retake) a seat; flush, no commit. Raises NotJoinable with
    reason `session_full`, `running` (no valid token for an existing
    seat) or `over`."""
    row = await db.get(SessionPlayer, (session.id, user.id))
    if session.status == "lobby":
        if row is None:
            seated = await db.scalar(
                select(func.count())
                .select_from(SessionPlayer)
                .where(SessionPlayer.session_id == session.id)
            )
            if seated >= config_from(session.config_overrides).max_players:
                raise NotJoinable("session_full")
            row = SessionPlayer(session_id=session.id, user_id=user.id)
            db.add(row)
        fresh = new_token()
        row.display_name = display_name
        row.token_hash = token_hash(fresh)
        await db.flush()
        return Seat(row, fresh)
    if session.status == "running":
        if row is not None and token is not None and secrets.compare_digest(
            row.token_hash, token_hash(token)
        ):
            return Seat(row, token)
        raise NotJoinable("running")
    raise NotJoinable("over")


async def authenticate(
    db: AsyncSession, join_code: str, token: str
) -> tuple[GameSession, SessionPlayer] | None:
    """The seat a token opens on the session behind `join_code`, or None."""
    row = (
        await db.execute(
            select(GameSession, SessionPlayer)
            .join(SessionPlayer, SessionPlayer.session_id == GameSession.id)
            .where(GameSession.join_code == join_code)
            .where(SessionPlayer.token_hash == token_hash(token))
        )
    ).first()
    return None if row is None else (row[0], row[1])
