"""Spec §4 — Generation:

    POST   /admin/generate/parse        → {params, notes} for the admin to confirm
    POST   /admin/generate              → 202 {job_id}
    GET    /admin/generate/{job_id}     → job status + counts
    GET    /admin/generate              → recent jobs

/parse is the one place free text meets a model synchronously: it returns
the §5.1 params it read out of the prompt and creates nothing. POST
validates the structured params the admin confirmed, inserts the
generation_jobs row, enqueues an arq job and returns. It never waits for
the model.
"""
import uuid

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.db import get_db
from app.models import GenerationJob
from app.queue import GENERATE_JOB, get_queue
from app.schemas.generation import (
    GenerationAccepted,
    GenerationJobRead,
    GenerationRequest,
    ParseRequest,
    ParseResponse,
)
from app.services.categories import get_category_by_slug, list_categories
from app.services.prompt_parser import ParseError, PromptParser

router = APIRouter(
    prefix="/admin/generate",
    tags=["admin: generation"],
    dependencies=[Depends(require_admin)],
)


def get_prompt_parser() -> PromptParser:
    """Dependency so tests can swap in a parser on a mocked transport."""
    try:
        return PromptParser.from_settings()
    except ParseError as exc:
        raise HTTPException(exc.status, detail=str(exc)) from exc


@router.post("/parse", response_model=ParseResponse)
async def parse_prompt(
    payload: ParseRequest,
    db: AsyncSession = Depends(get_db),
    parser: PromptParser = Depends(get_prompt_parser),
):
    slugs = [c.slug for c in await list_categories(db) if c.is_active]
    try:
        parsed = await parser.parse(payload.prompt, slugs)
    except ParseError as exc:
        raise HTTPException(exc.status, detail=str(exc)) from exc
    return ParseResponse(params=parsed.params, notes=parsed.notes)


@router.post("", response_model=GenerationAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    payload: GenerationRequest,
    db: AsyncSession = Depends(get_db),
    queue: ArqRedis = Depends(get_queue),
):
    category = await get_category_by_slug(db, payload.params.category_slug)
    if category is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown category slug: {payload.params.category_slug}",
        )

    job = GenerationJob(
        kind=payload.kind,
        prompt=payload.prompt,
        params=payload.params.model_dump(mode="json"),
        requested_count=payload.params.count,
    )
    db.add(job)
    await db.commit()

    # Commit first so the worker can always find the row. `_job_id` makes a
    # retry of this request a no-op in arq rather than a second run.
    try:
        await queue.enqueue_job(GENERATE_JOB, str(job.id), _job_id=f"{GENERATE_JOB}:{job.id}")
    except Exception as exc:  # redis down, etc.
        job.status = "failed"
        job.error = f"enqueue failed: {exc}"
        await db.commit()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="could not enqueue generation job"
        ) from exc
    return GenerationAccepted(job_id=job.id)


@router.get("", response_model=list[GenerationJobRead])
async def list_jobs(
    limit: int = Query(default=50, ge=1, le=200), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(GenerationJob).order_by(GenerationJob.created_at.desc()).limit(limit)
    )
    return list(result.scalars())


@router.get("/{job_id}", response_model=GenerationJobRead)
async def get_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    job = await db.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="job not found")
    return job
