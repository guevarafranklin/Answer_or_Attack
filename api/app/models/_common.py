"""Column shapes and enum vocabularies shared across model modules.

The vocabularies here are the source of truth for the CHECK constraints in
migration 0001 and for the Literal types in app/schemas. Keep them in sync
with the spec (§3); changing one requires a migration.
"""
import uuid
from datetime import datetime
from typing import Annotated

from sqlalchemy import CheckConstraint, DateTime, Uuid, text
from sqlalchemy.orm import mapped_column

LOCALES = ("en", "es")
REGIONS = ("us", "latam", "global")
ROLES = ("player", "admin")
SUBSCRIPTION_PLATFORMS = ("ios", "android", "promo")
SUBSCRIPTION_STATUSES = ("active", "grace", "expired", "refunded")
STUDY_PACK_STATUSES = ("uploading", "processing", "ready", "failed")
GENERATION_KINDS = ("category", "study_pack")
GENERATION_STATUSES = ("queued", "running", "succeeded", "partial", "failed")
GRADE_BANDS = ("g1_g3", "g4_g6", "g7_g9", "g10_g12", "adult")
QUESTION_STATUSES = ("pending", "live", "archived", "rejected")
QUESTION_SOURCES = ("seed", "ai", "user", "manual")
SERVE_OUTCOMES = ("correct", "incorrect", "timeout", "absent")
REPORT_REASONS = ("wrong_answer", "typo", "offensive", "confusing", "other")
SESSION_MODES = ("house", "study")
SESSION_STATUSES = ("lobby", "running", "finished", "abandoned")

# `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
UuidPk = Annotated[
    uuid.UUID,
    mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()")),
]
# `TIMESTAMPTZ NOT NULL DEFAULT now()`
TimestampNow = Annotated[
    datetime, mapped_column(DateTime(timezone=True), server_default=text("now()"))
]
# `TIMESTAMPTZ` (nullable)
Timestamp = Annotated[datetime | None, mapped_column(DateTime(timezone=True))]


def one_of(column: str, values: tuple[str, ...], *, name: str) -> CheckConstraint:
    """`CHECK (column IN ('a','b'))`, named as Postgres names inline checks
    (`<table>_<column>_check`) so it matches what migration 0001 created."""
    quoted = ",".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)
