"""stats rollup bookkeeping (§3.1, §9 step 7)

Revision ID: 0003_stats_rollup
Revises: 0002_generation_job_stats
Create Date: 2026-09-25

`rollup_watermarks` holds the high-water mark of the incremental rollup
(the last question_serves.id folded into question_stats), so each run only
reads the serves that arrived since the previous one.

`question_stats.avg_response_ms` cannot be advanced incrementally from the
average alone, so the rollup also keeps the running sum and the number of
serves that had a response time; avg_response_ms stays derived from them
and is what the health views read.

`absents` keeps the 'absent' serves (question shown, player gone) out of
`serves`: they say nothing about the question, so they must not dilute the
correct/timeout ratios or count toward the health views' serves floor.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_stats_rollup"
down_revision: Union[str, Sequence[str], None] = "0002_generation_job_stats"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE_SQL = """
CREATE TABLE rollup_watermarks (
  name       TEXT PRIMARY KEY,
  last_id    BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE question_stats
  ADD COLUMN absents           INT    NOT NULL DEFAULT 0,
  ADD COLUMN timed_serves      INT    NOT NULL DEFAULT 0,
  ADD COLUMN response_ms_total BIGINT NOT NULL DEFAULT 0;
"""

DOWNGRADE_SQL = """
ALTER TABLE question_stats
  DROP COLUMN response_ms_total,
  DROP COLUMN timed_serves,
  DROP COLUMN absents;

DROP TABLE IF EXISTS rollup_watermarks;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
