"""arq queue handle for the API side (spec §4: POST /admin/generate queues a
job and returns immediately).

One pool per process, created on first use so importing the app never
touches Redis. Tests override `get_queue` with a recorder."""
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.config import settings

GENERATE_JOB = "generate_questions"  # arq function name, see app.workers

_pool: ArqRedis | None = None


async def get_queue() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _pool
