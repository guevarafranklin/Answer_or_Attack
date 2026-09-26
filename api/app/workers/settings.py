"""arq WorkerSettings. The job functions get their collaborators from `ctx`
(populated here on startup) so tests can call them directly with a test
session and a hand-built generator, without Redis or a running worker."""
from typing import Any

from arq.connections import RedisSettings

from app.config import settings
from app.db import SessionLocal, engine
from app.services.generator import get_generator
from app.workers.generation import generate_questions


async def startup(ctx: dict[str, Any]) -> None:
    ctx["session_factory"] = SessionLocal
    ctx["generator"] = get_generator()


async def shutdown(ctx: dict[str, Any]) -> None:
    await engine.dispose()


class WorkerSettings:
    functions = [generate_questions]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown
    # One job at a time per worker: a generation job is a long, model-bound
    # loop and we don't want two of them racing on the house-hash index.
    max_jobs = 1
    job_timeout = 60 * 30
