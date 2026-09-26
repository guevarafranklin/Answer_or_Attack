"""Spec §4 — Categories:

    GET    /admin/categories
    POST   /admin/categories            {slug, icon, translations:{en:{...}, es:{...}}}
    PATCH  /admin/categories/{id}
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.db import get_db
from app.schemas.content import CategoryCreate, CategoryRead, CategoryUpdate
from app.services import categories as svc

router = APIRouter(
    prefix="/admin/categories",
    tags=["admin: categories"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=list[CategoryRead])
async def list_categories(db: AsyncSession = Depends(get_db)):
    return await svc.list_categories(db)


@router.post("", response_model=CategoryRead, status_code=status.HTTP_201_CREATED)
async def create_category(payload: CategoryCreate, db: AsyncSession = Depends(get_db)):
    try:
        category = await svc.create_category(db, payload)
    except svc.DuplicateSlug as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    return category


@router.patch("/{category_id}", response_model=CategoryRead)
async def update_category(
    category_id: uuid.UUID, payload: CategoryUpdate, db: AsyncSession = Depends(get_db)
):
    category = await svc.get_category(db, category_id)
    if category is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="category not found")
    category = await svc.update_category(db, category, payload)
    await db.commit()
    return category
