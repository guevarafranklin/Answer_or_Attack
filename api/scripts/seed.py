"""Seed the six house categories with EN/ES names (spec §8).

    cd api && python scripts/seed.py

Idempotent: a category whose slug already exists is left completely alone —
names, descriptions, icon and sort order edited through the admin API are
never overwritten. Only missing slugs are created.
"""
import asyncio
import sys
from pathlib import Path

# Allow `python scripts/seed.py` from api/ without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.schemas.content import CategoryCreate  # noqa: E402
from app.services.categories import create_category, get_category_by_slug  # noqa: E402

SEED_CATEGORIES: list[CategoryCreate] = [
    CategoryCreate(
        slug="math",
        icon="🔢",
        sort_order=0,
        translations={
            "en": {"name": "Math", "description": "Arithmetic, algebra, geometry and more"},
            "es": {"name": "Matemáticas", "description": "Aritmética, álgebra, geometría y más"},
        },
    ),
    CategoryCreate(
        slug="bible",
        icon="📖",
        sort_order=1,
        translations={
            "en": {
                "name": "Bible",
                "description": "People, places and stories of the Old and New Testament",
            },
            "es": {
                "name": "Biblia",
                "description": "Personajes, lugares e historias del Antiguo y Nuevo Testamento",
            },
        },
    ),
    CategoryCreate(
        slug="world-history",
        icon="🏛️",
        sort_order=2,
        translations={
            "en": {
                "name": "World History",
                "description": "Civilizations, leaders, wars and turning points",
            },
            "es": {
                "name": "Historia Universal",
                "description": "Civilizaciones, líderes, guerras y momentos decisivos",
            },
        },
    ),
    CategoryCreate(
        slug="music",
        icon="🎵",
        sort_order=3,
        translations={
            "en": {"name": "Music", "description": "Artists, songs, instruments and genres"},
            "es": {"name": "Música", "description": "Artistas, canciones, instrumentos y géneros"},
        },
    ),
    CategoryCreate(
        slug="cars",
        icon="🚗",
        sort_order=4,
        translations={
            "en": {"name": "Cars", "description": "Brands, models, engines and motorsport"},
            "es": {"name": "Autos", "description": "Marcas, modelos, motores y automovilismo"},
        },
    ),
    CategoryCreate(
        slug="science",
        icon="🔬",
        sort_order=5,
        translations={
            "en": {
                "name": "Science",
                "description": "Physics, chemistry, biology and the natural world",
            },
            "es": {
                "name": "Ciencia",
                "description": "Física, química, biología y el mundo natural",
            },
        },
    ),
]


async def seed(db: AsyncSession) -> list[str]:
    """Create any seed category whose slug is missing. Returns the slugs
    created this run (empty when everything already exists)."""
    created: list[str] = []
    for data in SEED_CATEGORIES:
        if await get_category_by_slug(db, data.slug) is not None:
            continue
        await create_category(db, data)
        created.append(data.slug)
    return created


async def main() -> None:
    async with SessionLocal() as db:
        created = await seed(db)
        await db.commit()
    if created:
        print(f"created {len(created)} categories: {', '.join(created)}")
    else:
        print("all seed categories already present; nothing to do")


if __name__ == "__main__":
    asyncio.run(main())
