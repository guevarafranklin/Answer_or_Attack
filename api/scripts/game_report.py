"""A playtest report for one session, from its event log (app.game.report).

    cd api && python scripts/game_report.py <join code or session id> [--json | --html] [--database-url URL]

Reads only: the session row (resolved config, overrides, seed), its
session_questions and session_events. Prints, per question, the stem
length, the timer used and every player's outcome and response time; per
attack window, how long each token holder took to attack or pass and how
many let it expire; and overall the timeout rate and p50/p90 response
time — with the timers the game ran on, from `resolved_config`. Exits 1
when there is no such session, it never started, or the log has a gap.
`--json` prints the report as JSON, `--html` the same page as
GET /dev/sessions/{join_code}/report.
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
from app.game import report as rep  # noqa: E402
from app.models import GameSession  # noqa: E402
from app.services import sessions as svc  # noqa: E402


async def main(args: argparse.Namespace) -> int:
    engine = create_async_engine(args.database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            try:
                session = await db.get(GameSession, uuid.UUID(args.session))
            except ValueError:
                session = await db.scalar(
                    select(GameSession).where(GameSession.join_code == args.session.upper())
                )
            if session is None:
                print(f"no session {args.session!r}", file=sys.stderr)
                return 1
            try:
                report = await rep.load_report(db, session)
            except svc.NotStarted:
                print(f"session {session.join_code} never started: nothing to report", file=sys.stderr)
                return 1
            except rep.ReplayError as exc:
                print(exc, file=sys.stderr)
                return 1
    finally:
        await engine.dispose()

    if args.json:
        print(json.dumps(rep.to_dict(report), indent=2))
    elif args.html:
        print(rep.render_html(report))
    else:
        sys.stdout.write(rep.render_text(report))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session", help="join code or session id (UUID)")
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="print the report as JSON")
    fmt.add_argument("--html", action="store_true", help="print the report as the dev HTML page")
    parser.add_argument("--database-url", default=settings.database_url)
    sys.exit(asyncio.run(main(parser.parse_args())))
