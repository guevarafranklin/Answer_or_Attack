"""generation_jobs.stats (§5.2 step 6 diagnostics)

Revision ID: 0002_generation_job_stats
Revises: 0001_initial_schema
Create Date: 2026-09-25

Structured per-job diagnostics: rejection reasons with counts, repeat_rate,
chunk failures. `error` stays a human-readable summary; the admin UI groups
on this column instead of parsing text.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_generation_job_stats"
down_revision: Union[str, Sequence[str], None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE generation_jobs ADD COLUMN stats JSONB NOT NULL DEFAULT '{}'::jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE generation_jobs DROP COLUMN stats")
