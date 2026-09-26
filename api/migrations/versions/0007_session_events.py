"""Phase 2 event log: session_events (§6)

Revision ID: 0007_session_events
Revises: 0006_session_player_tokens
Create Date: 2026-09-26

session_events
  Every event the engine applied, in order, with the server time it was
  scored at: the runtime's applied-event counter is `seq`, unique per
  session, and `payload` is the engine event's fields. Written by a
  background writer (app.game.persistence), never on the game's own
  path. With sessions.rng_seed and sessions.resolved_config, replaying
  the rows through the pure engine reproduces the game exactly
  (app.game.replay, scripts/replay_session.py). Rows go with their
  session.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_session_events"
down_revision: Union[str, Sequence[str], None] = "0006_session_player_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE_SQL = """
CREATE TABLE session_events (
  id          BIGSERIAL PRIMARY KEY,
  session_id  UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq         INT NOT NULL,
  at_ms       BIGINT NOT NULL,
  kind        TEXT NOT NULL,
  payload     JSONB NOT NULL,
  UNIQUE (session_id, seq)
);
"""

DOWNGRADE_SQL = """
DROP TABLE session_events;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
