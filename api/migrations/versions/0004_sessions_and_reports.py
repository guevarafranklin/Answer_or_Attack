"""session option order + one report per user per question (§4, §9 step 8)

Revision ID: 0004_sessions_and_reports
Revises: 0003_stats_rollup
Create Date: 2026-09-25

`session_questions.option_order` is the per-session shuffle of a question's
four options: position i of what the client saw holds the question's
original option option_order[i]. Stored with the session so the server can
score an answer against questions.correct_index (§4 Session content).

`question_reports_one_per_user_idx` is the "1 per user per question" rate
limit from §4 Player reports, enforced by the database so two concurrent
reports cannot both get through. Anonymous reports (user_id NULL, Phase 4)
are not limited by it.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_sessions_and_reports"
down_revision: Union[str, Sequence[str], None] = "0003_stats_rollup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE_SQL = """
ALTER TABLE session_questions
  ADD COLUMN option_order SMALLINT[] NOT NULL DEFAULT '{0,1,2,3}';

CREATE UNIQUE INDEX question_reports_one_per_user_idx
  ON question_reports (question_id, user_id) WHERE user_id IS NOT NULL;
"""

DOWNGRADE_SQL = """
DROP INDEX IF EXISTS question_reports_one_per_user_idx;

ALTER TABLE session_questions DROP COLUMN option_order;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
