"""Phase 2 live sessions: per-session player tokens, starting_xp set at END (§4, §6)

Revision ID: 0006_session_player_tokens
Revises: 0005_session_question_pools
Create Date: 2026-09-26

session_players.token_hash
  POST /sessions/{join_code}/join hands the player an opaque random
  `player_token`; only its sha256 hex is stored, so a DB read cannot be
  replayed as a WebSocket credential. Scoped to the session: the same
  user in two sessions has two rows and two tokens. The unique index is
  what the WebSocket handshake looks the token up by. Rows written before
  this migration get a random digest nobody holds a preimage for, so the
  column can be NOT NULL without opening any old seat.

session_players.starting_xp
  Was NOT NULL because Phase 1 created the row at generate time with the
  start already known. Now the row is created at join, in the lobby, and
  the secret start is drawn by the engine at Start and written back with
  final_xp/delta_xp when the game ends (§6, END persistence). NULL means
  "not ended yet".
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_session_player_tokens"
down_revision: Union[str, Sequence[str], None] = "0005_session_question_pools"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE_SQL = """
ALTER TABLE session_players
  ADD COLUMN token_hash TEXT,
  ALTER COLUMN starting_xp DROP NOT NULL;

UPDATE session_players
   SET token_hash = encode(sha256(gen_random_uuid()::text::bytea), 'hex')
 WHERE token_hash IS NULL;

ALTER TABLE session_players ALTER COLUMN token_hash SET NOT NULL;

CREATE UNIQUE INDEX session_players_token_hash_idx
    ON session_players (session_id, token_hash);
"""

DOWNGRADE_SQL = """
DROP INDEX IF EXISTS session_players_token_hash_idx;

-- Seats that never reached END have no start to keep; Phase 1 wrote it
-- at generate time, so the column was never NULL before this migration.
DELETE FROM session_players WHERE starting_xp IS NULL;

ALTER TABLE session_players
  ALTER COLUMN starting_xp SET NOT NULL,
  DROP COLUMN token_hash;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
