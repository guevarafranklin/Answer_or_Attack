"""Phase 2 spec §4 — session lifecycle over HTTP:

    POST /sessions
      {category_ids:[], locale, region, mode, pack_id?, config_overrides:{}}
      → 201 {session_id, join_code}
    POST /sessions/{join_code}/join
      {display_name, player_token?} → 200 {player_id, player_token}

`config_overrides` is any subset of GameConfig fields (question_count
lives there); it goes through GameConfig.from_overrides, so an unknown
key or a bad value is a 422 with the ConfigError message. The caller
(X-User-Id) becomes the session host. The lobby has no questions yet:
the draw happens when the host starts the game (§6,
app.services.sessions.draw_at_start, driven by the runtime).

Join takes a seat (app.services.players): the host joins like anyone
else. The token it returns opens the WebSocket (app.routers.ws); once
the game is running only a rejoin with that token is accepted (409
otherwise).
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_player
from app.db import get_db
from app.models import User
from app.schemas.sessions import (
    JoinRequest,
    JoinResponse,
    SessionCreateRequest,
    SessionCreateResponse,
)
from app.services import players
from app.services import sessions as svc

router = APIRouter(prefix="/sessions", tags=["player: sessions"])

_REFUSALS = {
    "session_full": "the session is full",
    "running": "the game has already started; rejoin with your player_token",
    "over": "the session has ended",
}


@router.post("", response_model=SessionCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreateRequest,
    player: User = Depends(current_player),
    db: AsyncSession = Depends(get_db),
):
    try:
        session = await svc.create_session(db, payload, player)
    except svc.PackRequired:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="pack_id is required in study mode and not allowed in house mode",
        )
    except svc.BadConfig as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"config_overrides: {exc}"
        )
    await db.commit()
    return SessionCreateResponse(session_id=session.id, join_code=session.join_code)


@router.post("/{join_code}/join", response_model=JoinResponse)
async def join_session(
    join_code: str,
    payload: JoinRequest,
    player: User = Depends(current_player),
    db: AsyncSession = Depends(get_db),
):
    session = await players.session_by_join_code(db, join_code)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no session with that join code")
    try:
        seat = await players.join(
            db, session, player, payload.display_name, token=payload.player_token
        )
    except players.NotJoinable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=_REFUSALS[exc.reason])
    await db.commit()
    return JoinResponse(player_id=seat.player_id, player_token=seat.token)
