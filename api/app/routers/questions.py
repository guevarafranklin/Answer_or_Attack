"""Spec §4 — Review queue:

    GET    /admin/questions?status=pending&category=math&locale=en&page=1
    GET    /admin/questions/{id}
    PATCH  /admin/questions/{id}        edit stem/options/correct_index/difficulty/region/tags
    POST   /admin/questions/{id}/approve     → status='live', stamps reviewed_by/at
    POST   /admin/questions/{id}/reject      → status='rejected'
    POST   /admin/questions/{id}/archive     → status='archived'
    POST   /admin/questions/bulk             {ids:[], action:'approve'|'reject'|'archive'}
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.db import get_db
from app.models import Question
from app.schemas.content import (
    BulkAction,
    QuestionBulkAction,
    QuestionBulkFailure,
    QuestionBulkResult,
    QuestionListQuery,
    QuestionPage,
    QuestionRead,
    QuestionUpdate,
)
from app.services import questions as svc

router = APIRouter(
    prefix="/admin/questions",
    tags=["admin: review queue"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=QuestionPage)
async def list_questions(
    query: Annotated[QuestionListQuery, Query()], db: AsyncSession = Depends(get_db)
):
    items, total = await svc.list_questions(db, query)
    return QuestionPage(items=items, page=query.page, page_size=query.page_size, total=total)


# Declared before /{question_id} so "bulk" is never parsed as an id.
@router.post("/bulk", response_model=QuestionBulkResult)
async def bulk_action(payload: QuestionBulkAction, db: AsyncSession = Depends(get_db)):
    updated, failed = await svc.apply_bulk(db, payload.ids, payload.action)
    await db.commit()
    return QuestionBulkResult(
        updated=updated,
        failed=[QuestionBulkFailure(id=id_, detail=detail) for id_, detail in failed],
    )


@router.get("/{question_id}", response_model=QuestionRead)
async def get_question(question_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await _load(db, question_id)


@router.patch("/{question_id}", response_model=QuestionRead)
async def update_question(
    question_id: uuid.UUID, payload: QuestionUpdate, db: AsyncSession = Depends(get_db)
):
    question = await _load(db, question_id)
    try:
        question = await svc.update_question(db, question, payload)
    except svc.DuplicateQuestion as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    return question


@router.post("/{question_id}/approve", response_model=QuestionRead)
async def approve_question(question_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await _act(db, question_id, "approve")


@router.post("/{question_id}/reject", response_model=QuestionRead)
async def reject_question(question_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await _act(db, question_id, "reject")


@router.post("/{question_id}/archive", response_model=QuestionRead)
async def archive_question(question_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await _act(db, question_id, "archive")


async def _load(db: AsyncSession, question_id: uuid.UUID) -> Question:
    question = await svc.get_question(db, question_id)
    if question is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="question not found")
    return question


async def _act(db: AsyncSession, question_id: uuid.UUID, action: BulkAction) -> Question:
    question = await _load(db, question_id)
    try:
        question = await svc.apply_action(db, question, action)
    except svc.QuestionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    return question
