"""Spec §4 — Player reports:

    POST /questions/{id}/report   {reason, note?, session_id?}

Player-facing (X-User-Id, see app.auth.current_player), not /admin.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import current_player
from app.db import get_db
from app.models import User
from app.schemas.telemetry import QuestionReportCreate, QuestionReportRead
from app.services import reports as svc

router = APIRouter(prefix="/questions", tags=["player: reports"])


@router.post(
    "/{question_id}/report",
    response_model=QuestionReportRead,
    status_code=status.HTTP_201_CREATED,
)
async def report_question(
    question_id: uuid.UUID,
    payload: QuestionReportCreate,
    player: User = Depends(current_player),
    db: AsyncSession = Depends(get_db),
):
    try:
        report = await svc.report_question(db, question_id, player, payload)
    except svc.QuestionNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="question not found")
    except svc.AlreadyReported:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="you already reported this question"
        )
    await db.commit()
    return report
