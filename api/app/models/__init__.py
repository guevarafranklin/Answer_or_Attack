"""ORM models for the spec §3 schema.

Importing this package registers every table on `Base.metadata`, which is
what Alembic autogenerate (and `alembic check`) compares against the DB.
"""
from app.db import Base
from app.models.content import (
    Category,
    CategoryTranslation,
    Question,
    QuestionTranslation,
    StudyPack,
)
from app.models.generation import GenerationJob
from app.models.sessions import GameSession, SessionPlayer, SessionQuestion
from app.models.telemetry import QuestionReport, QuestionServe, QuestionStats
from app.models.users import Subscription, TicketLedgerEntry, User

__all__ = [
    "Base",
    "Category",
    "CategoryTranslation",
    "GameSession",
    "GenerationJob",
    "Question",
    "QuestionReport",
    "QuestionServe",
    "QuestionStats",
    "QuestionTranslation",
    "SessionPlayer",
    "SessionQuestion",
    "StudyPack",
    "Subscription",
    "TicketLedgerEntry",
    "User",
]
