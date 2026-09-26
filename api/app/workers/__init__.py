"""arq worker (spec §2, §3.1, §5.2): generation jobs and the stats rollup
cron. Run with:

    cd api && arq app.workers.WorkerSettings
"""
from app.workers.settings import WorkerSettings

__all__ = ["WorkerSettings"]
