"""Phase 2 spec §4 — session lifecycle over HTTP:

    POST /sessions
      {category_ids:[], locale, region, mode, pack_id?, config_overrides:{}}
      → 201 {session_id, join_code}

`config_overrides` is any subset of GameConfig fields (question_count
lives there); it goes through GameConfig.from_overrides, so an unknown
key or a bad value is a 422 with the ConfigError message. The caller
(X-User-Id) becomes the session host. The lobby has no
questions yet: the draw happens when the host starts the game (§6,
app.services.sessions.draw_at_start, driven by the runtime). Join and the
WebSocket endpoint arrive with the runtime (§10 step 5).
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_player
from app.db import get_db
from app.models import User
from app.schemas.sessions import SessionCreateRequest, SessionCreateResponse
from app.services import sessions as svc

router = APIRouter(prefix="/sessions", tags=["player: sessions"])


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
