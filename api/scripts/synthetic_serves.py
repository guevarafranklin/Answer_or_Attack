"""Write realistic question_serves data to the TEST database and roll it up,
so the §4 health views have something to show (spec §8: "Health views
return sensible rows against synthetic question_serves data").

    cd api && python scripts/synthetic_serves.py [--questions 60] [--days 30]
                                                 [--seed 1] [--reset]

Targets `<DATABASE_URL database>_test` — the same scratch DB the test suite
uses — and refuses any other name, so it can never touch the dev database.
The test DB is created and migrated if needed. Note that the next pytest
session re-creates the schema from scratch, wiping this data; just run the
script again.

What it writes: the six seed categories, `--questions` live house questions
tagged "synthetic" (both locales), one archetype each (normal / easy /
wrong key / reported / dead / unproven), serves through the real ingestion
path in sessions of ~15 questions with a pool of players, question_reports
for the reported ones, then the rollup. `--reset` first deletes the
synthetic questions (serves, stats and reports cascade) and truncates the
telemetry tables and the rollup watermark.

To browse the result through the API:

    DATABASE_URL=<test url> uvicorn app.main:app --port 8001
    curl -H "Authorization: Bearer $ADMIN_TOKEN" localhost:8001/admin/health/suspect
"""
import argparse
import asyncio
import random
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import delete, func, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings  # noqa: E402
from app.models import Category, Question, QuestionReport, QuestionTranslation, User  # noqa: E402
from app.schemas.telemetry import QuestionServeCreate  # noqa: E402
from app.services.categories import create_category, get_category_by_slug  # noqa: E402
from app.services.health import health_counts  # noqa: E402
from app.services.serves import record_serves  # noqa: E402
from app.services.validation import content_hash  # noqa: E402
from app.workers.stats import run_rollup  # noqa: E402
from scripts.seed import SEED_CATEGORIES  # noqa: E402

SYNTHETIC_TAG = "synthetic"
PLAYERS = 40
QUESTIONS_PER_SESSION = 15
PLAYERS_PER_SESSION = (2, 6)


@dataclass(frozen=True)
class Archetype:
    """How a question behaves in play. Probabilities are per serve; what is
    left after correct/incorrect/timeout is 'absent' (player never answered
    and never timed out — left mid-round), which the rollup keeps out of
    `serves`, so the ranges below are a little above the 50 floor."""

    name: str
    weight: int  # share of the bank
    serves: tuple[int, int]  # per-question serve count range, absents included
    p_correct: float
    p_incorrect: float
    p_timeout: float
    response_ms: tuple[int, int]  # mean, sd for answered serves
    reports: tuple[int, int] = (0, 0)


ARCHETYPES = [
    Archetype("normal", 55, (60, 300), 0.62, 0.30, 0.05, (4200, 1500)),
    Archetype("easy", 12, (60, 300), 0.94, 0.03, 0.02, (2600, 900)),
    Archetype("wrong key", 8, (60, 200), 0.12, 0.80, 0.05, (3900, 1400), reports=(0, 3)),
    Archetype("reported", 6, (60, 150), 0.55, 0.35, 0.06, (4500, 1500), reports=(4, 12)),
    Archetype("dead", 9, (60, 200), 0.28, 0.05, 0.65, (8500, 1200)),
    Archetype("unproven", 10, (3, 49), 0.60, 0.30, 0.05, (4200, 1500)),
]

STEMS = {
    "en": "Synthetic question {n} ({archetype}): which option is marked correct?",
    "es": "Pregunta sintética {n} ({archetype}): ¿qué opción está marcada como correcta?",
}


def test_database_url() -> str:
    """`<dev db>_test`, the scratch DB the test suite uses (tests/conftest.py)."""
    dev = make_url(settings.database_url)
    url = dev.set(database=f"{dev.database}_test")
    assert url.database.endswith("_test")  # belt and braces: never the dev DB
    return url.render_as_string(hide_password=False)


def ensure_test_database(url: str) -> None:
    parsed = make_url(url)
    maint = parsed.set(database="postgres", drivername="postgresql")
    with psycopg.connect(maint.render_as_string(hide_password=False), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (parsed.database,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{parsed.database}"')
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")


async def reset(db: AsyncSession) -> None:
    await db.execute(
        text(
            "TRUNCATE question_serves, question_stats, question_reports, rollup_watermarks"
        )
    )
    result = await db.execute(delete(Question).where(Question.tags.any(SYNTHETIC_TAG)))
    print(f"reset: removed {result.rowcount} synthetic questions and all telemetry")


async def ensure_categories(db: AsyncSession) -> list[Category]:
    categories = []
    for data in SEED_CATEGORIES:
        category = await get_category_by_slug(db, data.slug)
        categories.append(category or await create_category(db, data))
    return categories


async def ensure_players(db: AsyncSession, rng: random.Random) -> list[User]:
    existing = select(User).where(User.display_name.like("synthetic player %"))
    players = list((await db.execute(existing)).scalars())
    for i in range(len(players), PLAYERS):
        user = User(display_name=f"synthetic player {i}", locale=rng.choice(["en", "es"]))
        db.add(user)
        players.append(user)
    await db.flush()
    return players


def pick_archetype(rng: random.Random) -> Archetype:
    return rng.choices(ARCHETYPES, weights=[a.weight for a in ARCHETYPES])[0]


async def make_questions(
    db: AsyncSession, categories: list[Category], count: int, rng: random.Random
) -> list[tuple[Question, Archetype]]:
    existing = await db.scalar(
        select(func.count()).select_from(Question).where(Question.tags.any(SYNTHETIC_TAG))
    )
    made = []
    for n in range(existing + 1, existing + count + 1):
        archetype = pick_archetype(rng)
        q = Question(
            category_id=rng.choice(categories).id,
            difficulty=rng.randint(1, 5),
            region="global",
            correct_index=rng.randrange(4),
            status="live",
            source="seed",
            tags=[SYNTHETIC_TAG, archetype.name.replace(" ", "_")],
            content_hash=content_hash(STEMS["en"].format(n=n, archetype=archetype.name)),
            translations=[
                QuestionTranslation(
                    locale=locale,
                    stem=STEMS[locale].format(n=n, archetype=archetype.name),
                    options=[f"Option {i + 1}" for i in range(4)],
                    explanation=None,
                )
                for locale in ("en", "es")
            ],
        )
        db.add(q)
        made.append((q, archetype))
    await db.flush()
    return made


def outcome_for(archetype: Archetype, rng: random.Random) -> tuple[str, int | None]:
    r = rng.random()
    if r < archetype.p_correct:
        outcome = "correct"
    elif r < archetype.p_correct + archetype.p_incorrect:
        outcome = "incorrect"
    elif r < archetype.p_correct + archetype.p_incorrect + archetype.p_timeout:
        return "timeout", None
    else:
        return "absent", None
    mean, sd = archetype.response_ms
    return outcome, int(min(max(rng.gauss(mean, sd), 400), 9900))


def make_serves(
    questions: list[tuple[Question, Archetype]], players: list[User], days: int, rng: random.Random
) -> list[QuestionServeCreate]:
    """Each question gets its archetype's serve count, dealt out as game
    sessions: a session shows ~15 questions to 2–6 players."""
    remaining = {q.id: rng.randint(*a.serves) for q, a in questions}
    archetype_of = {q.id: a for q, a in questions}
    now = datetime.now(timezone.utc)
    serves: list[QuestionServeCreate] = []
    while remaining:
        session_id = uuid.uuid4()
        locale = rng.choice(["en", "es"])
        started = now - timedelta(minutes=rng.uniform(5, days * 24 * 60))
        table = rng.sample(players, rng.randint(*PLAYERS_PER_SESSION))
        drawn = rng.sample(list(remaining), min(QUESTIONS_PER_SESSION, len(remaining)))
        for ordinal, question_id in enumerate(drawn):
            for player in table:
                if remaining.get(question_id, 0) <= 0:
                    break
                outcome, response_ms = outcome_for(archetype_of[question_id], rng)
                serves.append(
                    QuestionServeCreate(
                        question_id=question_id,
                        session_id=session_id,
                        user_id=player.id,
                        locale=locale,
                        outcome=outcome,
                        response_ms=response_ms,
                        served_at=started + timedelta(seconds=15 * ordinal + rng.uniform(0, 12)),
                    )
                )
                remaining[question_id] -= 1
            if remaining.get(question_id, 0) <= 0:
                remaining.pop(question_id, None)
    return serves


async def make_reports(
    db: AsyncSession,
    questions: list[tuple[Question, Archetype]],
    players: list[User],
    rng: random.Random,
) -> dict[uuid.UUID, int]:
    counts: dict[uuid.UUID, int] = {}
    for q, archetype in questions:
        n = rng.randint(*archetype.reports)
        if not n:
            continue
        counts[q.id] = n
        for _ in range(n):
            db.add(
                QuestionReport(
                    question_id=q.id,
                    user_id=rng.choice(players).id,
                    reason=rng.choice(["wrong_answer", "confusing", "typo"]),
                )
            )
    await db.flush()
    return counts


async def main(args: argparse.Namespace, url: str) -> None:
    rng = random.Random(args.seed)
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            if args.reset:
                await reset(db)
            categories = await ensure_categories(db)
            players = await ensure_players(db, rng)
            questions = await make_questions(db, categories, args.questions, rng)
            serves = make_serves(questions, players, args.days, rng)
            await record_serves(db, serves)
            reports = await make_reports(db, questions, players, rng)
            await db.commit()

            result = await run_rollup(db)
            # Phase 1's report endpoint increments question_stats.reports as
            # reports arrive; here they are set after the rollup created the rows.
            for question_id, n in reports.items():
                await db.execute(
                    text("UPDATE question_stats SET reports = :n WHERE question_id = :id"),
                    {"n": n, "id": question_id},
                )
            await db.commit()
            counts = await health_counts(db)
    finally:
        await engine.dispose()

    by_archetype = {}
    for _, archetype in questions:
        by_archetype[archetype.name] = by_archetype.get(archetype.name, 0) + 1
    print(f"test DB: {make_url(url).database}")
    print(f"questions: {len(questions)} " + ", ".join(f"{k} {v}" for k, v in by_archetype.items()))
    print(f"serves written: {len(serves)}; reports: {sum(reports.values())}")
    print(f"rollup: {result.batches} batches, {result.serves} serves, caught_up={result.caught_up}")
    print(f"health views: easy {counts.easy}, suspect {counts.suspect}, dead {counts.dead}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--questions", type=int, default=60, help="synthetic questions to add")
    parser.add_argument("--days", type=int, default=30, help="spread serves over the last N days")
    parser.add_argument("--seed", type=int, default=1, help="random seed (same seed, same data)")
    parser.add_argument("--reset", action="store_true", help="wipe synthetic data first")
    test_url = test_database_url()
    ensure_test_database(test_url)  # alembic runs its own event loop: before asyncio.run
    asyncio.run(main(parser.parse_args(), test_url))
