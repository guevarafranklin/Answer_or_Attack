"""Shared test fixtures.

Tests run against a dedicated scratch database (`<dev db name>_test`) derived
from settings.database_url. It is created if missing and migrated to head
once per session, so the schema under test is always the real Alembic
migration chain, never `create_all`.
"""
import psycopg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

ALEMBIC_INI = "alembic.ini"

_dev_url = make_url(settings.database_url)
TEST_DB_NAME = f"{_dev_url.database}_test"
TEST_URL = _dev_url.set(database=TEST_DB_NAME)


def _ensure_test_database_exists() -> None:
    maint = _dev_url.set(database="postgres", drivername="postgresql")
    with psycopg.connect(maint.render_as_string(hide_password=False), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')


def alembic_config() -> Config:
    cfg = Config(ALEMBIC_INI)
    # configparser interpolation: a literal '%' in the URL must be doubled.
    cfg.set_main_option(
        "sqlalchemy.url", TEST_URL.render_as_string(hide_password=False).replace("%", "%%")
    )
    return cfg


@pytest.fixture(scope="session")
def migrated_db() -> str:
    """Scratch DB migrated to head. Returns its URL."""
    _ensure_test_database_exists()
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return TEST_URL.render_as_string(hide_password=False)


@pytest_asyncio.fixture
async def db(migrated_db: str):
    """AsyncSession on the scratch DB. Each test runs in a transaction that is
    rolled back at the end, so tests never see each other's rows."""
    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = async_sessionmaker(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")()
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()
    await engine.dispose()
