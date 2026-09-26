"""Replay a session from its event log through the pure engine (§6, §9).

    cd api && python scripts/replay_session.py <session id or join code> [--json] [--database-url URL]

Reads only: the session row (resolved config, rng seed), session_questions
and session_events. Prints the final phase, round, results and serve
count — the state the live game had after its last logged event — and
exits 1 when the log cannot be replayed (unknown session, never started,
a gap in the log). `--json` prints the whole final state instead, in the
snapshot's JSON shape (app.game.snapshot.dump_state), so two replays or a
replay and a live snapshot can be diffed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import settings  # noqa: E402
from app.game import snapshot  # noqa: E402
from app.game.replay import ReplayError, replay  # noqa: E402
from app.models import GameSession  # noqa: E402


async def main(args: argparse.Namespace) -> int:
    engine = create_async_engine(args.database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            try:
                session_id = uuid.UUID(args.session)
            except ValueError:
                session_id = await db.scalar(
                    select(GameSession.id).where(GameSession.join_code == args.session.upper())
                )
                if session_id is None:
                    print(f"no session with join code {args.session!r}", file=sys.stderr)
                    return 1
            try:
                state = await replay(db, session_id)
            except ReplayError as exc:
                print(exc, file=sys.stderr)
                return 1
    finally:
        await engine.dispose()

    if args.json:
        print(json.dumps(snapshot.dump_state(state), indent=2))
        return 0
    print(f"session {session_id}: {state.phase.value}, round {state.round}, {len(state.serves)} serves")
    if state.end_reason is not None:
        print(f"ended: {state.end_reason}")
    for r in state.results or []:
        print(
            f"  {r.display_name} ({r.player_id}): {r.starting_xp} -> {r.final_xp}"
            f" (delta {r.delta:+d}, nominal {r.nominal_delta:+d})"
        )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session", help="session id (UUID) or join code")
    parser.add_argument("--json", action="store_true", help="print the final state as JSON")
    parser.add_argument("--database-url", default=settings.database_url)
    sys.exit(asyncio.run(main(parser.parse_args())))
