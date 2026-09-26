"""Spec §4 — the game socket:

    WS /ws/sessions/{join_code}?token=<player_token>

The token (from POST /sessions/{join_code}/join) is the only credential:
it names the seat, and the seat names the player. The handshake is
refused — closed before accept, with a code and reason — when the token
opens no seat on that session (4001) or the session is not live (4004:
ended, or running in a process that no longer holds it). After that the
handler is a pump: every frame goes to the runtime's queue, and the
socket closing is a `Disconnect` there. The runtime decides everything
else — Join versus Reconnect, replacing an older socket for the same
seat, what each player may see.

The DB is touched once, for the handshake, in its own short session:
holding a connection for the life of a socket would tie the player
count to the pool size.
"""
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app import db
from app.game import runtime
from app.services import players

router = APIRouter(tags=["player: game socket"])

CLOSE_BAD_TOKEN = 4001
CLOSE_NOT_LIVE = runtime.CLOSE_OVER


@router.websocket("/ws/sessions/{join_code}")
async def game_socket(ws: WebSocket, join_code: str, token: str = Query(default="")):
    async with db.SessionLocal() as session:
        found = await players.authenticate(session, join_code, token) if token else None
        if found is None:
            await ws.close(CLOSE_BAD_TOKEN, "invalid token for this session")
            return
        game, seat = found
        rt = await runtime.registry.get_or_load(join_code, game)
        as_host = seat.user_id == game.host_id
        player_id, display_name = players.player_id_of(seat.user_id), seat.display_name
    if rt is None:
        await ws.close(CLOSE_NOT_LIVE, "this session is not live")
        return
    await ws.accept()
    rt.connect(player_id, display_name, ws, as_host=as_host)
    try:
        while True:
            rt.receive(player_id, await ws.receive_text())
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError: the runtime closed this socket (replaced) between
        # two frames; either way the socket is done.
        pass
    finally:
        rt.disconnect(player_id, ws)
