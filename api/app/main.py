from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.auth import verify_admin_user
from app.config import settings
from app.game import persistence, runtime
from app.routers import categories, generation, health, questions, reports, sessions, ws

# Live games write their snapshot and event log through these hooks.
runtime.registry.hooks = persistence.Persistence()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Fail fast on a bad ADMIN_USER_ID (see verify_admin_user). SessionLocal is
    # looked up through the module at call time so tests can point it at their
    # rolled-back session.
    if settings.admin_user_id is not None:
        async with db.SessionLocal() as session:
            await verify_admin_user(session)
    # Games that were running when the previous process stopped continue
    # from their Redis snapshots; those without one are abandoned.
    await persistence.resume(runtime.registry)
    yield
    await runtime.registry.shutdown()


app = FastAPI(title="Answer or Attack — Content API", lifespan=lifespan)
app.include_router(categories.router)
app.include_router(generation.router)
app.include_router(questions.router)
app.include_router(health.router)
app.include_router(reports.router)
app.include_router(sessions.router)
app.include_router(ws.router)


@app.get("/health")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
