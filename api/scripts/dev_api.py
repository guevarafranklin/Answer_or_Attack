"""A second API on the test database, for the scripts that drive the game
from outside (smoke_dev_client.py, bots.py): never the dev API on :8000.

`start_api` creates and migrates `<DATABASE_URL db>_test` if needed, seeds
the six categories and enough synthetic live questions, then spawns
uvicorn with DATABASE_URL pointing there and ENV=dev (the dev routes are
what the scripts use to make guest users). The caller stops the process.
"""
import asyncio
import os
import random
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API_DIR))
sys.path.insert(0, str(API_DIR / "scripts"))

SYNTHETIC_QUESTIONS = 60  # ~10 per category: enough for question_count 3 with one category


def lan_ip() -> str:
    """The address a phone on the same Wi-Fi would use (not 127.0.0.1)."""
    try:
        out = subprocess.run(
            ["ipconfig", "getifaddr", "en0"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    # Portable fallback: the interface that routes to the internet.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]


def wait_http(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.25)
    return False


def start_api(
    port: int, log_path: Path, *, questions: int = SYNTHETIC_QUESTIONS, host: str = "0.0.0.0"
) -> tuple[subprocess.Popen, str]:
    """Second API on the seeded test DB. Returns (process, db url)."""
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Question
    from synthetic_serves import (
        SYNTHETIC_TAG,
        ensure_categories,
        ensure_test_database,
        make_questions,
        test_database_url,
    )

    url = test_database_url()
    ensure_test_database(url)  # create + migrate; alembic runs its own loop

    async def seed() -> None:
        engine = create_async_engine(url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                categories = await ensure_categories(db)
                have = await db.scalar(
                    select(func.count()).select_from(Question).where(Question.tags.any(SYNTHETIC_TAG))
                )
                if have < questions:
                    await make_questions(db, categories, questions - have, random.Random(1))
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(seed())

    env = {**os.environ, "DATABASE_URL": url, "ENV": "dev"}
    log = log_path.open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port)],
        cwd=API_DIR,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return proc, url
