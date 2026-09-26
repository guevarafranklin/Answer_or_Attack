"""arq worker (spec §2, §5.2). Run with:

    cd api && arq app.workers.WorkerSettings
"""
from app.workers.settings import WorkerSettings

__all__ = ["WorkerSettings"]
