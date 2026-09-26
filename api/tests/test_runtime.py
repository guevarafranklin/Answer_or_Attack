"""Phase 2 §10 step 5: the live session runtime — join, the game socket,
one task per session with a single ordered queue, the deadline timer,
presence, fan-out through the step 4 serializer.

These tests run the real app end to end with in-process WebSocket
clients (tests/asgi_ws.py). Because the runtime commits (the draw at
start, the status flip at the end) from sessions of its own, the usual
rolled-back test transaction cannot be shared with it: `live` gives the
runtime and the routes a committing session factory on the scratch DB
and deletes what the test created afterwards. Game time runs 25x faster
through the runtime's Clock, so a full game takes about a second.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import cache
from app import db as app_db
from app.auth import PLAYER_HEADER
from app.config import settings
from app.db import get_db
from app.game import engine as eng
from app.game import latency, runtime
from app.game.config import GameConfig
from app.main import app
from app.models import Category, GameSession, Question, QuestionReport, SessionPlayer, SessionQuestion, User
from app.services import players as players_svc
from tests.asgi_ws import Closed, WsClient
from tests.test_sessions import FakeRedis, _bank, _category

SPEED = 25.0
# Short games; every phase still has its full (scaled) window.
CONFIG = {"question_count": 3, "min_players": 2, "rejoin_seconds": 20, "abandon_seconds": 20}


class ManualClock(runtime.Clock):
    """Time moves only when the test says so; sleepers wake on `set`."""

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        super().__init__()
        self.t = start_ms
        self._changed = asyncio.Event()

    def now_ms(self) -> int:
        return self.t

    def set(self, at_ms: int) -> None:
        self.t = max(self.t, at_ms)
        self._changed.set()

    async def sleep_until(self, at_ms: int) -> None:
        while self.t < at_ms:
            self._changed.clear()
            await self._changed.wait()


@dataclass
class World:
    factory: async_sessionmaker[AsyncSession]
    redis: FakeRedis
    registry: runtime.Registry
    http: httpx.AsyncClient
    category_ids: list[uuid.UUID] = field(default_factory=list)
    user_ids: list[uuid.UUID] = field(default_factory=list)

    async def user(self, name: str) -> uuid.UUID:
        async with self.factory() as s:
            u = User(display_name=name)
            s.add(u)
            await s.commit()
            self.user_ids.append(u.id)
            return u.id

    async def bank(self, n: int = 16) -> uuid.UUID:
        async with self.factory() as s:
            cat = await _category(s)
            await _bank(s, cat, n)
            await s.commit()
            self.category_ids.append(cat.id)
            return cat.id

    async def lobby(self, host: uuid.UUID, *categories: uuid.UUID, **overrides: Any) -> str:
        body = {
            "locale": "en",
            "category_ids": [str(c) for c in categories],
            "config_overrides": {**CONFIG, **overrides},
        }
        r = await self.http.post("/sessions", json=body, headers={PLAYER_HEADER: str(host)})
        assert r.status_code == 201, r.text
        return r.json()["join_code"]

    async def join(self, user: uuid.UUID, code: str, name: str, **extra: Any) -> httpx.Response:
        return await self.http.post(
            f"/sessions/{code}/join",
            json={"display_name": name, **extra},
            headers={PLAYER_HEADER: str(user)},
        )

    async def seat(self, user: uuid.UUID, code: str, name: str) -> tuple[str, str]:
        r = await self.join(user, code, name)
        assert r.status_code == 200, r.text
        return r.json()["player_id"], r.json()["player_token"]

    async def connect(self, code: str, token: str) -> WsClient:
        return await WsClient(app, f"/ws/sessions/{code}", {"token": token}).open()

    async def session(self, code: str) -> GameSession:
        async with self.factory() as s:
            row = await s.scalar(select(GameSession).where(GameSession.join_code == code))
            assert row is not None
            return row

    def runtime(self, code: str) -> runtime.SessionRuntime:
        rt = self.registry.get(code)
        assert rt is not None
        return rt


@pytest_asyncio.fixture
async def live(migrated_db: str, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(migrated_db)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(app_db, "SessionLocal", factory)
    monkeypatch.setattr(settings, "env", "dev")
    fake = FakeRedis()
    monkeypatch.setattr(cache, "_client", fake)
    registry = runtime.Registry(clock=runtime.Clock(speed=SPEED))
    monkeypatch.setattr(runtime, "registry", registry)

    async def _get_db():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        world = World(factory, fake, registry, http)
        try:
            yield world
        finally:
            await registry.shutdown()
            app.dependency_overrides.pop(get_db, None)
            async with factory() as s:
                # Sessions first (session_questions has no cascade from
                # questions); reports, stats and serves cascade from questions.
                await s.execute(delete(GameSession).where(GameSession.host_id.in_(world.user_ids)))
                await s.execute(delete(Question).where(Question.category_id.in_(world.category_ids)))
                await s.execute(delete(Category).where(Category.id.in_(world.category_ids)))
                await s.execute(delete(User).where(User.id.in_(world.user_ids)))
                await s.commit()
    await engine.dispose()


async def settled(rt: runtime.SessionRuntime) -> None:
    """Everything enqueued so far has been applied — including what a
    task woken by the last item (a pinger, the timer) enqueued at once."""
    while True:
        await rt.queue.join()
        await asyncio.sleep(0)
        if rt.queue.empty():
            return


# ---------- a client that plays ----------


class Bot:
    """Plays one seat over a socket: picks the first category, answers
    option 0 (right sometimes, wrong sometimes), attacks the first other
    player whenever it holds a token, blocks with option 0. `lazy` never
    acts, so the timers carry it. Stops at `end`."""

    def __init__(self, world: World, code: str, player_id: str, token: str, *, lazy: bool = False):
        self.world, self.code, self.player_id, self.token, self.lazy = world, code, player_id, token, lazy
        self.ws: WsClient | None = None
        self.tokens = 0
        self.end: dict[str, Any] | None = None
        self.players: list[str] = []

    async def connect(self) -> WsClient:
        self.ws = await self.world.connect(self.code, self.token)
        return self.ws

    async def play(self, timeout: float = 20.0) -> dict[str, Any]:
        assert self.ws is not None
        async with asyncio.timeout(timeout):
            while self.end is None:
                await self.react(await self.ws.recv())
        return self.end

    async def react(self, m: dict[str, Any]) -> None:
        assert self.ws is not None
        match m["type"]:
            case "ping":  # even a lazy client echoes pings (§5)
                await self.ws.send({"type": "pong", "server_ms": m["server_ms"]})
            case "lobby" | "state":
                self.players = [p["player_id"] for p in m["players"]]
                if m["type"] == "state" and m["end"]:
                    self.end = m["end"]
            case "end":
                self.end = m
            case "reveal":
                self.tokens = m["tokens"]
        if self.lazy:
            return
        match m["type"]:
            case "board" if m["picker_id"] == self.player_id:
                await self.ws.send({"type": "pick", "category_id": m["category_ids"][0]})
            case "question" | "block_question":
                await self.ws.send({"type": "answer", "question_id": m["question_id"], "option": 0})
            case "phase" if m["phase"] == "attack":
                if self.tokens:
                    others = [p for p in self.players if p != self.player_id]
                    await self.ws.send({"type": "attack", "target_player_id": others[0]})


async def three_seats(world: World, **overrides: Any) -> tuple[str, list[Bot]]:
    """A lobby with three seated, connected players; the first is the host."""
    cat_a, cat_b = await world.bank(), await world.bank()
    users = [await world.user(n) for n in ("host", "beth", "carl")]
    code = await world.lobby(users[0], cat_a, cat_b, **overrides)
    bots = []
    for u, name in zip(users, ("host", "beth", "carl")):
        pid, token = await world.seat(u, code, name)
        assert pid == str(u)
        bots.append(Bot(world, code, pid, token))
    for b in bots:
        await b.connect()
    await settled(world.runtime(code))
    return code, bots


# ---------- join ----------


@pytest.mark.asyncio
async def test_join_hands_out_a_seat_and_a_session_scoped_hashed_token(live: World):
    cat = await live.bank()
    host, beth = await live.user("host"), await live.user("beth")
    code = await live.lobby(host, cat)
    other = await live.lobby(host, cat)

    r = await live.join(beth, code, "Beth")
    assert r.status_code == 200
    pid, token = r.json()["player_id"], r.json()["player_token"]
    assert pid == str(beth)
    assert len(token) >= 32 and token != pid

    async with live.factory() as s:
        row = await s.get(SessionPlayer, ((await live.session(code)).id, beth))
        assert row is not None
        assert row.token_hash == players_svc.token_hash(token) and token not in row.token_hash
        assert row.starting_xp is None  # drawn at Start, written at END
        assert await players_svc.authenticate(s, code, token) is not None
        assert await players_svc.authenticate(s, other, token) is None  # scoped
        assert await players_svc.authenticate(s, code, token[:-1] + "x") is None

    # Joining again in the lobby keeps the seat and rotates the token.
    again = await live.join(beth, code, "Beth again")
    assert again.status_code == 200 and again.json()["player_id"] == pid
    assert again.json()["player_token"] != token
    async with live.factory() as s:
        assert await players_svc.authenticate(s, code, token) is None

    assert (await live.join(beth, "NOPE99", "x")).status_code == 404
    bad = await live.http.post(f"/sessions/{code}/join", json={"display_name": ""}, headers={PLAYER_HEADER: str(beth)})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_join_respects_max_players(live: World):
    cat = await live.bank()
    host = await live.user("host")
    code = await live.lobby(host, cat, max_players=2)
    assert (await live.join(host, code, "host")).status_code == 200
    assert (await live.join(await live.user("b"), code, "b")).status_code == 200
    full = await live.join(await live.user("c"), code, "c")
    assert full.status_code == 409 and "full" in full.json()["detail"]


# ---------- the socket handshake ----------


@pytest.mark.asyncio
async def test_socket_refuses_bad_tokens_before_accepting(live: World):
    cat = await live.bank()
    host = await live.user("host")
    code = await live.lobby(host, cat)
    _, token = await live.seat(host, code, "host")

    with pytest.raises(Closed) as exc:
        await live.connect(code, "not-a-token")
    assert exc.value.code == 4001
    with pytest.raises(Closed) as exc:
        await live.connect("NOPE99", token)
    assert exc.value.code == 4001
    with pytest.raises(Closed):
        await WsClient(app, f"/ws/sessions/{code}").open()  # no token at all
    assert live.registry.live_codes() == []  # nothing was started for them


@pytest.mark.asyncio
async def test_connecting_seats_the_player_and_sends_lobby_then_state(live: World):
    code, bots = await three_seats(live)
    host = bots[0].ws
    assert host is not None
    first = await host.recv_type("lobby")
    assert first["host_id"] == bots[0].player_id
    state = await host.recv_type("state")
    assert state["phase"] == "lobby" and state["you"] == {
        "tokens": 0, "streak": 0, "answered": False, "acted": False
    }
    assert "starting_xp_choices" not in state["config"]
    # The host sees each later arrival as a fresh roster.
    rosters = [m for m in host.log if m["type"] == "lobby"]
    assert [len(r["players"]) for r in rosters] == [1]
    await host.recv_type("lobby")
    await host.recv_type("lobby")
    rosters = [m for m in host.log if m["type"] == "lobby"]
    assert [len(r["players"]) for r in rosters] == [1, 2, 3]
    assert all("tokens" not in p for r in rosters for p in r["players"])


# ---------- a full game ----------


@pytest.mark.asyncio
async def test_a_full_game_from_join_to_end_over_three_sockets(live: World):
    code, bots = await three_seats(live)
    bots[2].lazy = True  # never picks or answers: the timers carry them
    host = bots[0].ws
    assert host is not None
    await host.recv_type("state")
    await host.send({"type": "start"})

    ends = await asyncio.gather(*(b.play() for b in bots))

    assert all(e == ends[0] for e in ends)
    end = ends[0]
    assert end["reason"] == "finished"
    assert sorted(r["player_id"] for r in end["results"]) == sorted(b.player_id for b in bots)
    for r in end["results"]:
        assert r["starting_xp"] in (10, 12, 15)
        assert r["final_xp"] - r["starting_xp"] == r["delta"]
    assert end["winner_ids"]

    # Every player saw three rounds, and nothing secret before the end.
    for b in bots:
        assert b.ws is not None
        types = [m["type"] for m in b.ws.log]
        assert types.count("question") == 3 and types.count("reveal") == 3
        assert types[-1] == "end"
        for m in b.ws.log[:-1]:
            blob = str(m)
            assert "starting_xp" not in blob and "final_xp" not in blob
            if m["type"] == "question":
                assert "correct" not in blob
    # The lazy player timed out every round; the others got answer acks.
    lazy_log = bots[2].ws.log  # type: ignore[union-attr]
    assert [m["outcome"] for m in lazy_log if m["type"] == "reveal"] == ["timeout"] * 3
    assert all(m["accepted"] for m in bots[1].ws.log if m["type"] == "answer_ack")  # type: ignore[union-attr]

    # start ran the draw, committed it, cached it and flipped the status;
    # the end flipped it again. The runtime is gone with the game.
    row = await live.session(code)
    assert row.status == "finished" and row.started_at and row.ended_at and row.rng_seed is not None
    assert row.resolved_config["question_count"] == 3
    async with live.factory() as s:
        drawn = (await s.scalars(select(SessionQuestion).where(SessionQuestion.session_id == row.id))).all()
    assert len({q.pool for q in drawn}) == 3  # two categories + the block reserve
    assert cache.session_questions_key(row.id) in live.redis.store
    assert live.registry.get(code) is None
    for b in bots:
        with pytest.raises(Closed) as exc:
            await b.ws.recv(timeout=1)  # type: ignore[union-attr]
        assert exc.value.code == runtime.CLOSE_OVER


# ---------- start ----------


@pytest.mark.asyncio
async def test_only_the_host_can_start(live: World):
    code, bots = await three_seats(live)
    beth = bots[1].ws
    assert beth is not None
    await beth.send({"type": "start"})
    err = await beth.recv_type("error")
    assert err["code"] == "not_host"
    await settled(live.runtime(code))
    assert live.runtime(code).state.phase is eng.Phase.LOBBY
    assert (await live.session(code)).status == "lobby"
    assert all(m["type"] != "phase" for b in bots for m in b.ws.log)  # type: ignore[union-attr]

    host = bots[0].ws
    assert host is not None
    await host.send({"type": "start"})
    phase = await host.recv_type("phase")
    assert phase["phase"] == "pick" and phase["round"] == 1
    assert (await live.session(code)).status == "running"
    # A second start is refused, and does not redraw.
    await host.send({"type": "start"})
    assert (await host.recv_type("error"))["code"] == "wrong_phase"


@pytest.mark.asyncio
async def test_start_needs_enough_players_present(live: World):
    cat = await live.bank()
    host = await live.user("host")
    code = await live.lobby(host, cat)
    _, token = await live.seat(host, code, "host")
    ws = await live.connect(code, token)
    await ws.recv_type("state")
    await ws.send({"type": "start"})
    assert (await ws.recv_type("error"))["code"] == "not_enough_players"
    assert (await live.session(code)).status == "lobby" and (await live.session(code)).rng_seed is None


# ---------- late join ----------


@pytest.mark.asyncio
async def test_late_join_is_refused_but_a_rejoin_with_the_token_is_not(live: World):
    code, bots = await three_seats(live)
    host = bots[0].ws
    assert host is not None
    # Dan takes a seat in the lobby but never connects before the start.
    dan = await live.user("dan")
    _, dan_token = await live.seat(dan, code, "dan")
    await host.recv_type("state")
    await host.send({"type": "start"})
    await host.recv_type("phase")

    late = await live.join(await live.user("erin"), code, "erin")
    assert late.status_code == 409 and "started" in late.json()["detail"]
    # Dan's seat exists in the DB but the engine never seated him.
    with pytest.raises(Closed) as exc:
        ws = await live.connect(code, dan_token)
        while True:
            await ws.recv()
    assert exc.value.code == runtime.CLOSE_REFUSED
    assert ws.log[-1]["type"] == "error" and ws.log[-1]["code"] == "wrong_phase"
    assert str(dan) not in live.runtime(code).state.players

    # A seated player rejoins over HTTP with the token they hold, and no other.
    beth = bots[1]
    ok = await live.join(uuid.UUID(beth.player_id), code, "beth", player_token=beth.token)
    assert ok.status_code == 200 and ok.json() == {"player_id": beth.player_id, "player_token": beth.token}
    assert (await live.join(uuid.UUID(beth.player_id), code, "beth")).status_code == 409
    assert (await live.join(uuid.UUID(beth.player_id), code, "beth", player_token="wrong")).status_code == 409
    assert (await live.join(uuid.UUID(beth.player_id), code, "beth", player_token=dan_token)).status_code == 409


# ---------- one socket per player ----------


@pytest.mark.asyncio
async def test_a_new_connection_replaces_the_old_one(live: World):
    code, bots = await three_seats(live)
    beth, host = bots[1], bots[0].ws
    assert host is not None
    old = beth.ws
    assert old is not None
    await old.recv_type("state")

    new = await live.connect(code, beth.token)
    with pytest.raises(Closed) as exc:
        while True:
            await old.recv()
    assert exc.value.code == runtime.CLOSE_REPLACED and "replaced" in exc.value.reason
    state = await new.recv_type("state")
    assert state["you"] is not None and state["phase"] == "lobby"
    await settled(live.runtime(code))
    # Beth was never absent: no presence change reached anyone, and the
    # old socket's own close does not count as her leaving.
    assert live.runtime(code).state.players[beth.player_id].present
    await old.close()
    await settled(live.runtime(code))
    assert live.runtime(code).state.players[beth.player_id].present
    assert live.runtime(code).sockets[beth.player_id] is not old  # the seat holds the new socket

    # The new socket works: messages from it are hers.
    await new.send({"type": "sync", "client_ms": 5})
    reply = await new.recv_type("sync_reply")
    assert reply["client_ms"] == 5 and reply["server_ms"] > 0
    await host.send({"type": "sync", "client_ms": 6})
    await host.recv_type("sync_reply")
    assert all(m["type"] != "presence" for m in host.log)


# ---------- disconnect and rejoin ----------


@pytest.mark.asyncio
async def test_disconnect_and_rejoin_mid_round_gets_a_correct_state(live: World):
    code, bots = await three_seats(live)
    host, beth, carl = bots
    for b in bots:
        await b.ws.recv_type("state")  # type: ignore[union-attr]
    await host.ws.send({"type": "start"})  # type: ignore[union-attr]
    # Get to QUESTION: whoever picks, picks.
    for b in bots:
        board = await b.ws.recv_type("board")  # type: ignore[union-attr]
        if board["picker_id"] == b.player_id:
            await b.ws.send({"type": "pick", "category_id": board["category_ids"][0]})  # type: ignore[union-attr]
    question = await host.ws.recv_type("question")  # type: ignore[union-attr]
    await host.ws.send({"type": "answer", "question_id": question["question_id"], "option": 1})  # type: ignore[union-attr]
    assert (await host.ws.recv_type("answer_ack"))["accepted"]  # type: ignore[union-attr]

    await beth.ws.close()  # type: ignore[union-attr]
    gone = await host.ws.recv_type("presence")  # type: ignore[union-attr]
    assert gone == {"type": "presence", "player_id": beth.player_id, "status": "absent"}
    rt = live.runtime(code)
    await settled(rt)
    assert rt.state.phase is eng.Phase.QUESTION and not rt.state.players[beth.player_id].present

    ws = await live.connect(code, beth.token)
    back = await host.ws.recv_type("presence")  # type: ignore[union-attr]
    assert back["status"] == "returned"
    state = await ws.recv_type("state")
    assert state["phase"] == "question" and state["round"] == 1
    assert state["question"]["question_id"] == question["question_id"]
    assert state["question"]["stem"] == question["stem"] and len(state["question"]["options"]) == 4
    assert state["deadline_ms"] == question["deadline_ms"]
    assert state["you"] == {"tokens": 0, "streak": 0, "answered": False, "acted": False}
    assert [p["player_id"] for p in state["players"]] == [b.player_id for b in bots]
    assert all(p["present"] for p in state["players"])
    assert "correct" not in str(state["question"]) and "starting_xp" not in str(state)
    assert state["end"] is None

    # She can still answer, and a second reconnect shows it.
    await ws.send({"type": "answer", "question_id": question["question_id"], "option": 2})
    assert (await ws.recv_type("answer_ack"))["accepted"]
    ws2 = await live.connect(code, beth.token)
    assert (await ws2.recv_type("state"))["you"]["answered"] is True
    # Carl answering too closes the question: everyone, Beth's new socket
    # included, gets the reveal.
    await carl.ws.send({"type": "answer", "question_id": question["question_id"], "option": 0})  # type: ignore[union-attr]
    reveal = await ws2.recv_type("reveal")
    assert reveal["outcome"] in ("correct", "incorrect") and set(reveal["deltas"]) == {b.player_id for b in bots}


@pytest.mark.asyncio
async def test_a_player_gone_too_long_is_dropped_by_the_timer(live: World):
    code, bots = await three_seats(live, rejoin_seconds=1)
    host, beth, _ = bots
    await host.ws.recv_type("state")  # type: ignore[union-attr]
    await host.ws.send({"type": "start"})  # type: ignore[union-attr]
    await host.ws.recv_type("phase")  # type: ignore[union-attr]
    await beth.ws.close()  # type: ignore[union-attr]
    assert (await host.ws.recv_type("presence"))["status"] == "absent"  # type: ignore[union-attr]
    dropped = await host.ws.recv_type("presence", timeout=3)  # type: ignore[union-attr]
    assert dropped == {"type": "presence", "player_id": beth.player_id, "status": "dropped"}
    with pytest.raises(Closed) as exc:  # a dropped seat cannot come back
        ws = await live.connect(code, beth.token)
        while True:
            await ws.recv()
    assert exc.value.code == runtime.CLOSE_REFUSED


@pytest.mark.asyncio
async def test_a_game_left_by_everyone_is_abandoned(live: World):
    code, bots = await three_seats(live, abandon_seconds=1, rejoin_seconds=1)
    host = bots[0].ws
    assert host is not None
    await host.recv_type("state")
    await host.send({"type": "start"})
    await host.recv_type("phase")
    await bots[1].ws.close()  # type: ignore[union-attr]
    await bots[2].ws.close()  # type: ignore[union-attr]
    end = await host.recv_type("end", timeout=3)
    assert end["reason"] == "abandoned" and len(end["results"]) == 3
    await asyncio.sleep(0.05)
    assert (await live.session(code)).status == "abandoned"
    assert live.registry.get(code) is None


# ---------- sync, report, bad messages ----------


@pytest.mark.asyncio
async def test_sync_report_and_bad_messages(live: World):
    code, bots = await three_seats(live)
    host = bots[0].ws
    assert host is not None
    await host.recv_type("state")

    await host.send("not json")
    assert (await host.recv_type("error"))["code"] == "bad_message"
    await host.send({"type": "answer", "question_id": "q", "option": 9})
    err = await host.recv_type("error")
    assert err["code"] == "bad_message" and "option" in err["message"]
    await host.send({"type": "pick", "category_id": "c"})
    assert (await host.recv_type("error"))["code"] == "wrong_phase"

    await host.send({"type": "sync", "client_ms": 123})
    reply = await host.recv_type("sync_reply")
    assert reply["client_ms"] == 123 and abs(reply["server_ms"] - live.registry.clock.now_ms()) < 60_000  # type: ignore[union-attr]

    # A report needs a question this player has been shown.
    await host.send({"type": "report", "question_id": str(uuid.uuid4()), "reason": "typo"})
    ack = await host.recv_type("report_ack")
    assert ack["accepted"] is False and ack["reason"] == "unknown_question"

    await host.send({"type": "start"})
    for b in bots:
        board = await b.ws.recv_type("board")  # type: ignore[union-attr]
        if board["picker_id"] == b.player_id:
            await b.ws.send({"type": "pick", "category_id": board["category_ids"][0]})  # type: ignore[union-attr]
    question = await host.recv_type("question")
    await host.send({"type": "report", "question_id": question["question_id"], "reason": "typo", "note": "sp"})
    ack = await host.recv_type("report_ack")
    assert ack == {"type": "report_ack", "question_id": question["question_id"], "accepted": True, "reason": None}
    await host.send({"type": "report", "question_id": question["question_id"], "reason": "typo"})
    assert (await host.recv_type("report_ack"))["reason"] == "already_reported"
    async with live.factory() as s:
        report = await s.scalar(select(QuestionReport).where(QuestionReport.question_id == uuid.UUID(question["question_id"])))
    assert report is not None and report.user_id == uuid.UUID(bots[0].player_id)
    assert report.session_id == (await live.session(code)).id and report.note == "sp"
    # Nobody else was shown a block question; the other players' acks are their own.
    assert all(m["type"] != "report_ack" for m in bots[1].ws.log)  # type: ignore[union-attr]


# ---------- ordering ----------


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed: tuple[int, str | None] | None = None

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["type"] == kind]


@dataclass
class Manual:
    """A runtime driven directly, on a clock the test moves by hand, with
    fake sockets: for anything where the exact millisecond matters."""

    rt: runtime.SessionRuntime
    clock: ManualClock
    socks: dict[str, FakeSocket]

    @property
    def pids(self) -> list[str]:
        return list(self.socks)

    def send(self, pid: str, message: dict[str, Any]) -> None:
        self.rt.receive(pid, json.dumps(message))

    async def tick_to(self, at_ms: int) -> None:
        self.clock.set(at_ms)
        await settled(self.rt)

    async def start(self) -> None:
        self.send(self.pids[0], {"type": "start"})
        await settled(self.rt)
        assert self.rt.state.phase is eng.Phase.PICK

    async def pick(self) -> str:
        """The picker picks the first category; the open question's id."""
        picker = self.rt.state.picker_id
        assert picker is not None
        board = self.socks[picker].of("board")[-1]
        self.send(picker, {"type": "pick", "category_id": board["category_ids"][0]})
        await settled(self.rt)
        assert self.rt.state.phase is eng.Phase.QUESTION and self.rt.state.question is not None
        return self.rt.state.question.id

    def answer(self, pid: str, qid: str, option: int = 0) -> None:
        self.send(pid, {"type": "answer", "question_id": qid, "option": option})

    async def pong(self, pid: str, index: int, delay_ms: int) -> None:
        """Echo this player's `index`-th ping after `delay_ms` of server
        time — advancing the clock in burst-gap steps until it is sent."""
        while len(self.socks[pid].of("ping")) <= index:
            await self.tick_to(self.clock.t + latency.PING_BURST_GAP_MS)
        stamp = self.socks[pid].of("ping")[index]["server_ms"]
        self.clock.set(stamp + delay_ms)
        self.send(pid, {"type": "pong", "server_ms": stamp})
        await settled(self.rt)


async def manual(live: World, **overrides: Any) -> Manual:
    cat = await live.bank()
    users = [await live.user(n) for n in ("a", "b", "c")]
    code = await live.lobby(users[0], cat, **overrides)
    session = await live.session(code)
    clock = ManualClock()
    rt = runtime.SessionRuntime(session.id, code, GameConfig.from_overrides(session.config_overrides), clock=clock)
    rt.start_task()
    socks: dict[str, FakeSocket] = {}
    for u, name in zip(users, "abc"):
        await live.seat(u, code, name)
        socks[str(u)] = FakeSocket()
        rt.connect(str(u), name, socks[str(u)], as_host=u == users[0])
    await settled(rt)
    return Manual(rt, clock, socks)


@pytest.mark.asyncio
async def test_events_in_the_same_millisecond_are_applied_in_queue_order(live: World):
    """Two attackers, one target, room for one attack: whoever's message
    was enqueued first wins, and a same-millisecond stamp changes nothing.
    Run twice with the arrival order swapped."""
    for first_wins in (0, 1):
        m = await manual(live, max_incoming_attacks=1, question_count=2)
        rt, clock, socks = m.rt, m.clock, m.socks
        a, b, c = m.pids
        await m.start()
        # Everyone holds a token, so the attack window opens after the reveal.
        for p in rt.state.players.values():
            p.tokens = 1
        qid = await m.pick()
        for pid in (a, b, c):
            m.answer(pid, qid)
        await settled(rt)
        assert rt.state.phase is eng.Phase.REVEAL
        await m.tick_to(rt.state.phase_end_ms)  # type: ignore[arg-type]
        assert rt.state.phase is eng.Phase.ATTACK

        order = (a, b) if first_wins == 0 else (b, a)
        for pid in order:
            rt.receive(pid, f'{{"type": "attack", "target_player_id": "{c}"}}')
        stamps = [item.at_ms for item in list(rt.queue._queue)]  # type: ignore[attr-defined]
        assert len(set(stamps)) == 1  # same millisecond, by construction
        await settled(rt)

        winner, loser = order
        assert [x["attacker_id"] for x in socks[c].of("attacks")[-1]["attacks"]] == [winner]
        assert socks[loser].of("error")[-1]["code"] == "target_full"
        assert not socks[winner].of("error")
        await rt.stop()


# ---------- §5: server-measured RTT ----------


@pytest.mark.asyncio
async def test_rtt_is_the_median_of_pongs_measured_on_the_server_clock(live: World):
    m = await manual(live)
    a, b, _ = m.pids
    # A burst of five pings goes out on connect, spaced by the burst gap.
    # a's client echoes at once; b's is slow, each pong coming back
    # after a simulated delay.
    for i, delay in enumerate([40, 60, 50, 45, 55]):
        await m.pong(a, i, 0)
        await m.pong(b, i, delay)
    assert len(m.socks[a].of("ping")) == latency.PING_BURST
    stamps = [p["server_ms"] for p in m.socks[a].of("ping")]
    assert stamps == sorted(set(stamps))  # distinct and increasing
    assert list(m.rt.latency[b].samples) == [40, 60, 50, 45, 55]
    assert m.rt._rtt_ms(b) == 50
    assert m.rt._rtt_ms(a) == 0  # answered instantly (in server time)

    # Then one every 15 s.
    await m.tick_to(m.clock.t + latency.PING_INTERVAL_MS)
    assert len(m.socks[a].of("ping")) == latency.PING_BURST + 1
    await m.pong(b, latency.PING_BURST, 30)
    assert list(m.rt.latency[b].samples) == [40, 60, 50, 45, 55, 30]
    # Pings and pongs are no game events: nobody was told anything.
    assert not any(sock.of("error") for sock in m.socks.values())
    await m.rt.stop()


@pytest.mark.asyncio
async def test_rtt_median_resists_one_outlier(live: World):
    m = await manual(live)
    a = m.pids[0]
    for i, delay in enumerate([40, 40, 900, 40, 40]):
        await m.pong(a, i, delay)
    assert m.rt._rtt_ms(a) == 40
    await m.rt.stop()


@pytest.mark.asyncio
async def test_pongs_for_unknown_or_stale_pings_are_ignored(live: World):
    m = await manual(live)
    a = m.pids[0]
    await m.pong(a, 0, 30)
    assert list(m.rt.latency[a].samples) == [30]
    sent = m.socks[a].of("ping")[0]["server_ms"]

    m.send(a, {"type": "pong", "server_ms": sent + 12_345})  # never sent
    m.send(a, {"type": "pong", "server_ms": sent})  # already echoed
    m.send(a, {"type": "pong", "server_ms": sent - 1})  # forged: would read as a negative trip
    await settled(m.rt)
    assert list(m.rt.latency[a].samples) == [30]

    # A ping left unanswered for too long is forgotten: echoing it late
    # cannot plant a huge sample.
    await m.pong(a, 1, 0)
    await m.tick_to(m.clock.t + latency.PING_BURST_GAP_MS)  # the third ping goes out ...
    stale = m.socks[a].of("ping")[2]["server_ms"]  # ... and is left unanswered
    await m.tick_to(stale + latency.STALE_MS + 1)
    m.send(a, {"type": "pong", "server_ms": stale})
    await settled(m.rt)
    assert list(m.rt.latency[a].samples) == [30, 0]
    assert not m.socks[a].of("error")
    await m.rt.stop()


@pytest.mark.asyncio
async def test_a_new_socket_measures_afresh_and_a_replaced_one_stops_being_pinged(live: World):
    m = await manual(live)
    a = m.pids[0]
    await m.pong(a, 0, 500)
    assert m.rt._rtt_ms(a) == 500
    old = m.socks[a]
    new = FakeSocket()
    m.rt.connect(a, "a", new, as_host=True)
    await settled(m.rt)
    assert old.closed is not None and old.closed[0] == runtime.CLOSE_REPLACED
    assert m.rt._rtt_ms(a) == 0 and len(new.of("ping")) == 1
    pings_to_old = len(old.of("ping"))
    for _ in range(latency.PING_BURST - 1):
        await m.tick_to(m.clock.t + latency.PING_BURST_GAP_MS)
    await m.tick_to(m.clock.t + latency.PING_INTERVAL_MS)
    assert len(old.of("ping")) == pings_to_old
    assert len(new.of("ping")) == latency.PING_BURST + 1
    await m.rt.stop()


@pytest.mark.asyncio
async def test_real_clients_are_pinged_on_connect_and_their_echo_is_measured(live: World):
    code, bots = await three_seats(live)
    host = bots[0].ws
    assert host is not None
    await host.recv_type("state")
    ping = await host.recv_type("ping")
    assert ping["server_ms"] > 0
    await host.send({"type": "pong", "server_ms": ping["server_ms"]})
    await host.send({"type": "sync", "client_ms": 1})
    await host.recv_type("sync_reply")
    rt = live.runtime(code)
    await settled(rt)
    assert len(rt.latency[bots[0].player_id].samples) == 1
    assert 0 <= rt._rtt_ms(bots[0].player_id) < 1000 * SPEED


# ---------- §5: acceptance at deadline + grace ----------


@pytest.mark.asyncio
async def test_answers_are_judged_by_their_enqueue_stamp(live: World):
    """An answer stamped at deadline+grace-1 on arrival is accepted even
    if the queue only gets to it after the window has passed."""
    m = await manual(live, grace_ms=1500)
    a, b, c = m.pids
    await m.start()
    qid = await m.pick()
    s = m.rt.state
    assert s.deadline_ms is not None and s.phase_end_ms == s.deadline_ms + 1500
    assert m.socks[a].of("question")[-1]["deadline_ms"] == s.deadline_ms
    end = s.phase_end_ms

    m.clock.set(end - 1)
    m.answer(a, qid)  # stamped end-1 on enqueue ...
    m.clock.set(end + 5_000)  # ... the clock moves on before it is applied
    await settled(m.rt)
    assert m.socks[a].of("answer_ack")[-1] == {"type": "answer_ack", "question_id": qid, "accepted": True, "reason": None}
    assert m.rt.state.answers[a].received_ms == end - 1
    # The timer's tick (stamped after the answer) then closed the question.
    assert m.rt.state.phase is eng.Phase.REVEAL
    assert m.socks[a].of("reveal")[-1]["outcome"] in ("correct", "incorrect")
    assert m.socks[b].of("reveal")[-1]["outcome"] == "timeout"
    await m.rt.stop()


@pytest.mark.asyncio
async def test_answer_at_deadline_plus_grace_minus_one_is_accepted_and_plus_one_is_too_late(live: World):
    m = await manual(live, grace_ms=400)
    a, b, c = m.pids
    await m.start()
    qid = await m.pick()
    end = m.rt.state.phase_end_ms
    assert end is not None and end == m.rt.state.deadline_ms + 400

    await m.tick_to(end - 1)
    m.answer(a, qid)
    await settled(m.rt)
    assert m.socks[a].of("answer_ack")[-1]["accepted"] is True
    assert m.rt.state.phase is eng.Phase.QUESTION  # b and c still may answer

    m.clock.set(end + 1)
    m.answer(b, qid)  # queued before the timer wakes for `end`
    await settled(m.rt)
    ack = m.socks[b].of("answer_ack")[-1]
    assert ack["accepted"] is False and ack["reason"] == "too_late"
    assert m.rt.state.phase is eng.Phase.REVEAL
    outcomes = {pid: m.socks[pid].of("reveal")[-1]["outcome"] for pid in (a, b, c)}
    assert outcomes[a] in ("correct", "incorrect") and outcomes[b] == outcomes[c] == "timeout"
    await m.rt.stop()


@pytest.mark.asyncio
async def test_the_question_ends_at_deadline_plus_grace_unless_everyone_answered(live: World):
    m = await manual(live, grace_ms=400, question_count=2)
    a, b, c = m.pids
    await m.start()
    qid = await m.pick()
    deadline, end = m.rt.state.deadline_ms, m.rt.state.phase_end_ms
    assert deadline is not None and end == deadline + 400
    # Nobody answers: the deadline itself changes nothing, the grace does.
    await m.tick_to(deadline)
    assert m.rt.state.phase is eng.Phase.QUESTION
    await m.tick_to(end - 1)
    assert m.rt.state.phase is eng.Phase.QUESTION
    await m.tick_to(end)
    assert m.rt.state.phase is eng.Phase.REVEAL
    assert m.socks[a].of("phase")[-1]["phase"] == "reveal"

    # Round 2: everyone answers well before the deadline, and the phase
    # closes at once.
    await m.tick_to(m.rt.state.phase_end_ms)  # type: ignore[arg-type]
    while m.rt.state.phase is not eng.Phase.PICK:  # no tokens were earned: no attack window
        await m.tick_to(m.rt.state.phase_end_ms)  # type: ignore[arg-type]
    assert m.rt.state.round == 2
    qid = await m.pick()
    deadline = m.rt.state.deadline_ms
    assert deadline is not None
    await m.tick_to(deadline - 5_000)
    for pid in (a, b, c):
        m.answer(pid, qid)
    await settled(m.rt)
    assert m.rt.state.phase is eng.Phase.REVEAL and m.clock.t == deadline - 5_000
    await m.rt.stop()


@pytest.mark.asyncio
async def test_response_time_takes_off_half_the_measured_rtt(live: World):
    m = await manual(live)
    a, b, c = m.pids
    for i in range(5):
        await m.pong(a, i, 100)  # a: RTT 100
        await m.pong(c, i, 5_000)  # c: a terrible link
    assert m.rt._rtt_ms(a) == 100 and m.rt._rtt_ms(b) == 0 and m.rt._rtt_ms(c) == 5_000
    await m.start()
    qid = await m.pick()
    sent = m.rt.state.question_sent_ms
    assert sent == m.clock.t
    await m.tick_to(sent + 1_000)
    for pid in (a, b, c):
        m.answer(pid, qid)
    await settled(m.rt)
    answers = m.rt.state.answers
    assert all(ans.received_ms == sent + 1_000 for ans in answers.values())
    assert answers[a].response_ms == 950
    assert answers[b].response_ms == 1_000
    assert answers[c].response_ms == 0  # floored, never negative
    await m.rt.stop()
