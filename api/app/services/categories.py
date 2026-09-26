"""Categories (spec §4): a category and its translations are always written
together. Functions here flush but do not commit; the caller decides the
transaction boundary."""
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Category, CategoryTranslation
from app.schemas.content import CategoryCreate, CategoryTranslationIn, CategoryUpdate


class DuplicateSlug(Exception):
    def __init__(self, slug: str):
        super().__init__(f"category slug already exists: {slug}")
        self.slug = slug


def _with_translations():
    return select(Category).options(selectinload(Category.translations))


async def list_categories(db: AsyncSession) -> list[Category]:
    result = await db.execute(_with_translations().order_by(Category.sort_order, Category.slug))
    return list(result.scalars())


async def get_category(db: AsyncSession, category_id: uuid.UUID) -> Category | None:
    result = await db.execute(_with_translations().where(Category.id == category_id))
    return result.scalar_one_or_none()


async def get_category_by_slug(db: AsyncSession, slug: str) -> Category | None:
    result = await db.execute(_with_translations().where(Category.slug == slug))
    return result.scalar_one_or_none()


async def create_category(db: AsyncSession, data: CategoryCreate) -> Category:
    """Insert the category and every translation in one flush. A slug
    collision raises DuplicateSlug after rolling back to the pre-call state
    (a savepoint), so the caller's transaction stays usable."""
    category = Category(
        slug=data.slug,
        icon=data.icon,
        sort_order=data.sort_order,
        translations=[
            CategoryTranslation(locale=locale, name=t.name, description=t.description)
            for locale, t in data.translations.items()
        ],
    )
    try:
        async with db.begin_nested():
            db.add(category)
    except IntegrityError as exc:
        if _constraint_name(exc) == "categories_slug_key":
            raise DuplicateSlug(data.slug) from exc
        raise
    await db.refresh(category, attribute_names=["id", "created_at", "is_active"])
    return category


async def update_category(db: AsyncSession, category: Category, data: CategoryUpdate) -> Category:
    """Apply only the fields the client sent. Translations are merged per
    locale: a locale that is sent is replaced, one that is omitted is kept."""
    fields = data.model_dump(exclude_unset=True)
    translations: dict[str, CategoryTranslationIn] = data.translations or {}
    for name, value in fields.items():
        if name != "translations":
            setattr(category, name, value)
    by_locale = {t.locale: t for t in category.translations}
    for locale, incoming in translations.items():
        if locale in by_locale:
            by_locale[locale].name = incoming.name
            by_locale[locale].description = incoming.description
        else:
            category.translations.append(
                CategoryTranslation(
                    locale=locale, name=incoming.name, description=incoming.description
                )
            )
    await db.flush()
    return category


def _constraint_name(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)
