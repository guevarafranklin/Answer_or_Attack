from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db():
    async with SessionLocal() as session:
        yield session

def constraint_name(exc: IntegrityError) -> str | None:
    """The Postgres constraint an IntegrityError tripped, so services can
    turn one specific violation into a domain error and re-raise the rest."""
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)
