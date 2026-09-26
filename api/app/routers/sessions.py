"""Spec §4 — Session content (consumed by the game in Phase 2):

    POST /sessions/generate
      {category_ids:[], locale, region, question_count, difficulty_curve, mode, pack_id?}
      → {session_id, questions:[{id, stem, options, ordinal}, ...], short_by?}

The caller (X-User-Id) becomes the session host. `correct_index` never
leaves the server: it lives in session_questions.option_order and in the
Redis copy at session:{id}:questions.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_player
from app.cache import SESSION_QUESTIONS_TTL, get_redis, session_questions_key
from app.db import get_db
from app.models import User
from app.schemas.sessions import (
    SessionGenerateRequest,
    SessionGenerateResponse,
    SessionQuestionOut,
)
from app.services import sessions as svc

log = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["player: sessions"])


@router.post(
    "/generate", response_model=SessionGenerateResponse, status_code=status.HTTP_201_CREATED
)
async def generate_session(
    payload: SessionGenerateRequest,
    player: User = Depends(current_player),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    try:
        session, cache, short_by = await svc.generate_session(db, payload, player)
    except svc.PackRequired:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="pack_id is required in study mode and not allowed in house mode",
        )
    # Commit first: the cache must never point at a session that does not
    # exist. The DB rows are the source of truth; a cache miss in Phase 2
    # means one query, not a lost session.
    await db.commit()
    try:
        await redis.set(
            session_questions_key(session.id),
            cache.model_dump_json(),
            ex=SESSION_QUESTIONS_TTL,
        )
    except Exception:  # redis down: the session still works
        log.warning("could not cache questions for session %s", session.id, exc_info=True)
    return SessionGenerateResponse(
        session_id=session.id,
        questions=[
            SessionQuestionOut(**q.model_dump(include=set(SessionQuestionOut.model_fields)))
            for q in cache.questions
        ],
        short_by=short_by,
    )
