"""Redis handle for the API side (spec §4: the session payload is cached at
session:{id}:questions for 2 hours so Phase 2 makes no per-round DB query).

One client per process, created on first use so importing the app never
touches Redis. Tests override `get_redis` with an in-memory fake. The arq
queue (app.queue) keeps its own pool; the two are independent."""
import uuid
from datetime import timedelta

from redis.asyncio import Redis

from app.config import settings

SESSION_QUESTIONS_TTL = timedelta(hours=2)

_client: Redis | None = None


def session_questions_key(session_id: uuid.UUID) -> str:
    return f"session:{session_id}:questions"


async def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(settings.redis_url)
    return _client
