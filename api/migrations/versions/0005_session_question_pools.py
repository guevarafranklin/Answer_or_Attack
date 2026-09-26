"""Phase 2 sessions: question pools, config overrides, resolved config, rng seed (§4, §6)

Revision ID: 0005_session_question_pools
Revises: 0004_sessions_and_reports
Create Date: 2026-09-26

session_questions.pool
  Phase 2 draws a pool per selected category (ramped easy → hard within
  the pool) plus a block reserve, instead of Phase 1's single ordered
  list. `pool` says which one a row belongs to: the category id as text,
  or 'block' for the reserve. `ordinal` stays unique per session and
  orders the rows inside their pool. Existing Phase 1 rows are backfilled
  from the question's category, so a lobby drawn before this migration
  still reads as one pool per category.

sessions.config_overrides
  What the host asked for at POST /sessions: a subset of GameConfig
  fields, validated by GameConfig.from_overrides. '{}' means defaults.

sessions.resolved_config
  The full GameConfig the game actually ran with, written at start after
  the player-count tier picked starting_xp_choices. NULL while in lobby.

sessions.rng_seed
  Set at start. The option shuffle in the draw and the engine's rng both
  derive from it, so a replay needs only this seed, resolved_config and
  the event log.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_session_question_pools"
down_revision: Union[str, Sequence[str], None] = "0004_sessions_and_reports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE_SQL = """
ALTER TABLE session_questions ADD COLUMN pool TEXT;

UPDATE session_questions sq
   SET pool = q.category_id::text
  FROM questions q
 WHERE q.id = sq.question_id;

ALTER TABLE session_questions
  ALTER COLUMN pool SET NOT NULL,
  ADD CONSTRAINT session_questions_pool_check
    CHECK (pool = 'block' OR pool ~ '^[0-9a-f-]{36}$');

CREATE INDEX session_questions_pool_idx ON session_questions (session_id, pool, ordinal);

ALTER TABLE sessions
  ADD COLUMN config_overrides JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN resolved_config JSONB,
  ADD COLUMN rng_seed BIGINT;
"""

DOWNGRADE_SQL = """
ALTER TABLE sessions
  DROP COLUMN rng_seed,
  DROP COLUMN resolved_config,
  DROP COLUMN config_overrides;

DROP INDEX IF EXISTS session_questions_pool_idx;

ALTER TABLE session_questions DROP COLUMN pool;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
