"""Phase 2 spec §7 — the throwaway web test client and what it needs to
run without accounts. Every route here answers 404 unless `ENV=dev`, so
in prod the whole prefix does not exist:

    GET  /dev/client                   the client (one HTML file, app/static)
    POST /dev/guest   {display_name}   a throwaway users row → {user_id}
    GET  /dev/categories               active categories with both names,
                                       for the host's picker and the board
    GET  /dev/sessions/{code}/report   the playtest report of a started
                                       game (app.game.report): timers used,
                                       per-question outcomes and response
                                       times, attack-window delays;
                                       ?format=json|text for the data

`/dev/guest` is what lets a playtester just type a name: the id it
returns is the `X-User-Id` the player stub (app.auth.current_player)
resolves on POST /sessions and /join. The categories list is the admin
one without the bearer token — there is no player-facing category route
yet (Phase 3), and the client needs names for the ids in `board`.
"""
from pathlib import Path

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.game import report as rep
from app.models import GameSession, User
from app.services import categories as svc
from app.services import sessions as sessions_svc

CLIENT_HTML = Path(__file__).resolve().parent.parent / "static" / "dev_client.html"


def require_dev() -> None:
    # Read at request time (like current_player) so tests can flip it.
    if settings.env != "dev":
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not Found")


router = APIRouter(prefix="/dev", tags=["dev"], dependencies=[Depends(require_dev)])


class GuestCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=40)


class GuestOut(BaseModel):
    user_id: str
    display_name: str


class DevCategory(BaseModel):
    id: str
    slug: str
    icon: str | None
    names: dict[str, str]  # by locale


@router.get("/client", response_class=HTMLResponse)
async def client_page() -> HTMLResponse:
    # Read on every request: edit the file, reload the phone, no restart.
    return HTMLResponse(CLIENT_HTML.read_text(encoding="utf-8"))


@router.post("/guest", response_model=GuestOut, status_code=status.HTTP_201_CREATED)
async def create_guest(payload: GuestCreate, db: AsyncSession = Depends(get_db)) -> GuestOut:
    user = User(display_name=payload.display_name.strip())
    db.add(user)
    await db.flush()
    await db.commit()
    return GuestOut(user_id=str(user.id), display_name=user.display_name)


@router.get("/categories", response_model=list[DevCategory])
async def list_categories(db: AsyncSession = Depends(get_db)) -> list[DevCategory]:
    return [
        DevCategory(
            id=str(c.id),
            slug=c.slug,
            icon=c.icon,
            names={t.locale: t.name for t in c.translations},
        )
        for c in await svc.list_categories(db)
        if c.is_active
    ]


@router.get("/sessions/{join_code}/report", response_class=HTMLResponse)
async def session_report(
    join_code: str,
    format: Literal["html", "json", "text"] = Query("html"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """404 for an unknown code, 409 for a lobby (no draw, no log yet),
    500 for a log the engine cannot replay (a gap)."""
    session = await db.scalar(select(GameSession).where(GameSession.join_code == join_code.upper()))
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not Found")
    try:
        report = await rep.load_report(db, session)
    except sessions_svc.NotStarted:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"session {session.join_code} has not started") from None
    except rep.ReplayError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from None
    if format == "json":
        return JSONResponse(rep.to_dict(report))
    if format == "text":
        return PlainTextResponse(rep.render_text(report))
    return HTMLResponse(rep.render_html(report))
