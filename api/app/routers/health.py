"""Spec §4 — Health views (question_stats only, serves >= 50 floor):

    GET /admin/health/easy       correct/serves > 0.90            ORDER BY ratio DESC
    GET /admin/health/suspect    correct/serves < 0.25 OR reports > 3
    GET /admin/health/dead       timeouts/serves > 0.60
    GET /admin/health/summary    counts by category/status/locale, pending backlog
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.db import get_db
from app.schemas.telemetry import HealthPage, HealthSummary, HealthView
from app.services import health as svc

router = APIRouter(
    prefix="/admin/health",
    tags=["admin: health"],
    dependencies=[Depends(require_admin)],
)


@router.get("/summary", response_model=HealthSummary)
async def summary(db: AsyncSession = Depends(get_db)):
    return await svc.health_summary(db)


@router.get("/{view}", response_model=HealthPage)
async def view(
    view: HealthView,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    db: AsyncSession = Depends(get_db),
):
    items, total = await svc.health_view(db, view, page=page, page_size=page_size)
    return HealthPage(view=view, items=items, page=page, page_size=page_size, total=total)
