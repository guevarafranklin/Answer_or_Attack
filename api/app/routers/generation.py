"""Spec §4 — Generation:

    POST   /admin/generate              → 202 {job_id}
    GET    /admin/generate/{job_id}     → job status + counts
    GET    /admin/generate              → recent jobs

POST validates the structured §5.1 params, inserts the generation_jobs row,
enqueues an arq job and returns. It never waits for the model.
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
from app.schemas.generation import GenerationAccepted, GenerationJobRead, GenerationRequest
from app.services.categories import get_category_by_slug

router = APIRouter(
    prefix="/admin/generate",
    tags=["admin: generation"],
    dependencies=[Depends(require_admin)],
)


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
