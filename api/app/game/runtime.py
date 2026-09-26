"""The live session runtime (spec §3): one asyncio task per session.

    socket ──┐
    socket ──┼─▶ inbound queue ─▶ SessionRuntime._run ─▶ engine.step
    timer ───┘        (strict order)        │
                                            ├─▶ protocol.fan_out ─▶ sockets
                                            └─▶ hooks (snapshot, event log,
                                                       END persistence)

Everything that can change the game — a socket message, a socket
opening or closing, a timer tick — is put on one queue and applied by
one task, so events are applied strictly in the order they arrived and
the engine never sees two at once. Each item is stamped with server time
when it is enqueued; that stamp is the `now_ms` the engine scores it at
(§5: the server stamps on receipt, client clocks are never trusted).

The runtime owns:

* the engine state and its rng (seeded from `sessions.rng_seed` at
  start);
* the phase deadline timer: after every step the next wake-up is the
  earliest of `phase_end_ms`, each absent player's rejoin deadline and
  the abandon deadline, and a `Tick` is queued when it comes;
* the connection registry — one socket per player; a new connection
  replaces the old one, which is closed with a clear reason;
* fan-out through the step 4 serializer (`protocol.fan_out`,
  `protocol.state_message`), the only place client payloads are built;
* `start`: the question draw (`draw_at_start`), the commit, the Redis
  cache, `status = running`, then the engine's `Start`;
* the RTT measurement (§5, `app.game.latency`): a pinger task per
  socket queues `ping` items (a burst on connect, then one every 15 s),
  the game task stamps and sends them, and a `pong` coming back through
  the queue is a sample stamped on receipt. The player's rolling median
  is what `Answer.rtt_ms` carries into the engine, where half of it
  comes off the recorded response time. It is never a scoring input;
* what is not a game event: `sync` is answered here, `report` goes
  through the report service;
* `resume`: continuing a game from its snapshot after a restart — the
  state, rng and event counter are restored, every player is marked
  absent through the engine, and the timers are re-armed from the
  stored deadlines.

Session status transitions (`lobby → running → finished | abandoned`)
are the runtime's (§6). The Redis snapshot and the event log are
`app.game.persistence`, the rest of END persistence (players' XP,
`record_serves`) is step 8: they plug into `RuntimeHooks`.

Time is read from a `Clock` so tests can run a whole game in a second
by speeding it up; nothing here calls `time` directly.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from app import cache, db
from app.game import engine as eng
from app.game import latency
from app.game import protocol as proto
from app.game.config import GameConfig
from app.game.engine import GameState, Phase
from app.game.seed import engine_rng
from app.game.snapshot import Snapshot
from app.models import GameSession, User
from app.schemas.telemetry import QuestionReportCreate
from app.services import reports
from app.services import sessions as svc

log = logging.getLogger(__name__)

# WebSocket close codes (4000-4999 are application-defined).
CLOSE_REPLACED = 4000  # a newer connection took this seat
CLOSE_REFUSED = 4003  # the engine would not seat this player (late join)
CLOSE_OVER = 4004  # the game has ended


# ---------- time ----------


class Clock:
    """Server time in epoch milliseconds. `speed` > 1 makes the whole game
    run faster (tests); the epoch origin is still real time, so stamps
    stay plausible in logs."""

    def __init__(self, speed: float = 1.0) -> None:
        self.speed = speed
        self._origin_ms = time.time_ns() // 1_000_000
        self._origin_mono = time.monotonic()

    def now_ms(self) -> int:
        elapsed = (time.monotonic() - self._origin_mono) * self.speed
        return self._origin_ms + int(elapsed * 1000)

    async def sleep_until(self, at_ms: int) -> None:
        while (remaining := at_ms - self.now_ms()) > 0:
            await asyncio.sleep(remaining / 1000 / self.speed)


# ---------- sockets ----------


class Socket(Protocol):
    """What the runtime needs from a connection (starlette.WebSocket fits)."""

    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


# ---------- hooks for steps 7 and 8 ----------


class RuntimeHooks:
    """Persistence seams. The defaults do nothing; `app.game.persistence`
    (Redis snapshot, event log) and step 8 (END persistence) override
    them. All are awaited inside the runtime task, so a slow hook slows
    the game — implementations that touch a store should hand off to a
    task."""

    async def on_event(
        self, rt: SessionRuntime, seq: int, at_ms: int, event: eng.Event, messages: list[eng.Message]
    ) -> None:
        """One applied event (the event log)."""

    async def on_state(self, rt: SessionRuntime, state: GameState) -> None:
        """The state after an event (the Redis snapshot)."""

    async def on_end(self, rt: SessionRuntime, ended: eng.Ended) -> None:
        """The game ended (players' XP and record_serves, step 8)."""

    async def on_shutdown(self) -> None:
        """The process is stopping and every runtime has been stopped:
        finish whatever is still in flight."""


# ---------- inbound queue ----------

ItemKind = Literal["connect", "disconnect", "message", "tick", "ping", "report_done"]


@dataclass(slots=True)
class _Item:
    kind: ItemKind
    at_ms: int
    player_id: str | None = None
    socket: Socket | None = None
    display_name: str = ""
    as_host: bool = False
    raw: str = ""
    payload: Any = None


# ---------- the runtime ----------


class SessionRuntime:
    def __init__(
        self,
        session_id: uuid.UUID,
        join_code: str,
        config: GameConfig,
        *,
        clock: Clock | None = None,
        hooks: RuntimeHooks | None = None,
        on_finish: Callable[[SessionRuntime], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.join_code = join_code
        self.clock = clock or Clock()
        self.hooks = hooks or RuntimeHooks()
        self._on_finish = on_finish  # registry callback
        # Lobby state: the config as overridden, no questions yet. `start`
        # swaps in the resolved config, the pools and the seeded rng.
        self.state: GameState = eng.new_game(config, {}, [])
        self.rng = engine_rng(0)  # unused before start; Join does not draw
        self.texts: dict[str, proto.QuestionText] = {}
        self.seq = 0  # applied-event counter, for the event log hook
        self.queue: asyncio.Queue[_Item] = asyncio.Queue()
        self.sockets: dict[str, Socket] = {}
        self.shown: dict[str, set[str]] = {}  # question ids each player has seen
        self.latency: dict[str, latency.RttTracker] = {}  # per connected player
        self.finished: bool = False
        self._task: asyncio.Task[None] | None = None
        self._timer: asyncio.Task[None] | None = None
        self._armed_for: int | None = None
        self._pingers: dict[str, asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[Any]] = set()

    # ----- lifecycle -----

    def start_task(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"session:{self.join_code}")

    async def stop(self) -> None:
        """Cancel the task and close every socket (process shutdown)."""
        for t in (self._timer, self._task, *self._pingers.values(), *self._background):
            if t is not None:
                t.cancel()
        for sock in list(self.sockets.values()):
            await self._close(sock, CLOSE_OVER, "server shutting down")
        self.sockets.clear()
        self._pingers.clear()

    async def resume(self, snap: Snapshot, texts: Mapping[str, proto.QuestionText]) -> None:
        """Pick a game up from its snapshot after a restart (§3): the
        state, the rng and the event counter continue where they were
        (the caller has already brought the snapshot up to the last
        logged event); nobody is connected, so every player is marked
        absent *through the engine* — a `Disconnect` at resume time for
        each present player, logged like any other event so a replay
        sees what the live game did — and the rejoin window starts now.
        Timers are re-armed from the stored deadlines: one already past
        ticks at once. A game the log had already ended is finished
        here, since its end never reached the DB. Call before
        `start_task`."""
        self.state = snap.state
        self.rng = snap.rng
        self.seq = snap.seq
        self.texts = dict(texts)
        self.shown = {pid: set(qids) for pid, qids in snap.shown.items()}
        if self.state.phase is Phase.END:
            await self._finish(eng.ended(self.state))
            return
        now = self.clock.now_ms()
        for p in list(self.state.players.values()):
            if p.present and not p.dropped:
                await self._apply(eng.Disconnect(p.id), now)
                if self.finished:
                    return
        self._arm_timer()

    # ----- inbound API (called from socket handlers; all just enqueue) -----

    def _put(self, kind: ItemKind, **fields: Any) -> None:
        self.queue.put_nowait(_Item(kind, self.clock.now_ms(), **fields))

    def connect(self, player_id: str, display_name: str, socket: Socket, *, as_host: bool) -> None:
        self._put("connect", player_id=player_id, display_name=display_name, socket=socket, as_host=as_host)

    def disconnect(self, player_id: str, socket: Socket) -> None:
        self._put("disconnect", player_id=player_id, socket=socket)

    def receive(self, player_id: str, raw: str) -> None:
        self._put("message", player_id=player_id, raw=raw)

    # ----- the task -----

    async def _run(self) -> None:
        try:
            while not self.finished:
                item = await self.queue.get()
                try:
                    await self._handle(item)
                except Exception:
                    log.exception("session %s: failed handling %s", self.join_code, item.kind)
                finally:
                    self.queue.task_done()  # so `queue.join()` means "all applied"
        except asyncio.CancelledError:
            raise
        finally:
            if self._timer is not None:
                self._timer.cancel()

    async def _handle(self, item: _Item) -> None:
        match item.kind:
            case "connect":
                await self._on_connect(item)
            case "disconnect":
                await self._on_disconnect(item)
            case "message":
                await self._on_message(item)
            case "tick":
                self._armed_for = None
                await self._apply(eng.Tick(), item.at_ms)
            case "ping":
                await self._on_ping(item)
            case "report_done":
                await self._send(item.player_id or "", item.payload)

    # ----- applying events -----

    async def _apply(self, event: eng.Event, at_ms: int) -> list[eng.Message]:
        self.state, messages = eng.step(self.state, event, at_ms, self.rng)
        self.seq += 1
        await self.hooks.on_event(self, self.seq, at_ms, event, messages)
        await self.hooks.on_state(self, self.state)
        outbox = proto.fan_out(self.state, messages, self.texts)
        for pid, msgs in outbox.items():
            for m in msgs:
                await self._send(pid, m)
        ended = next((m for m in messages if isinstance(m, eng.Ended)), None)
        if ended is not None:
            await self._finish(ended)
        else:
            self._arm_timer()
        return messages

    async def _send(self, player_id: str, message: proto.ServerMessage) -> None:
        sock = self.sockets.get(player_id)
        if sock is None:
            return
        if isinstance(message, (proto.QuestionOut, proto.BlockQuestionOut)):
            self.shown.setdefault(player_id, set()).add(message.question_id)
        try:
            await sock.send_text(message.model_dump_json())
        except Exception:
            # A dead socket counts as a close; the queued disconnect is
            # applied in order like any other (a duplicate from the
            # handler is ignored, see _on_disconnect).
            log.info("session %s: send to %s failed", self.join_code, player_id, exc_info=True)
            self.disconnect(player_id, sock)

    async def _close(self, sock: Socket, code: int, reason: str) -> None:
        try:
            await sock.close(code, reason)
        except Exception:
            pass

    # ----- timer -----

    def _next_wake_ms(self) -> int | None:
        s = self.state
        if s.phase is Phase.END:
            return None
        due: list[int] = []
        rejoin = s.config.rejoin_seconds * 1000
        for p in s.players.values():
            if not p.present and not p.dropped and p.absent_since_ms is not None:
                due.append(p.absent_since_ms + rejoin)
        if s.phase is not Phase.LOBBY:
            if s.phase_end_ms is not None:
                due.append(s.phase_end_ms)
            if s.low_presence_since_ms is not None:
                due.append(s.low_presence_since_ms + s.config.abandon_seconds * 1000)
        return min(due) if due else None

    def _arm_timer(self) -> None:
        wake = self._next_wake_ms()
        if wake == self._armed_for:
            return
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._armed_for = wake
        if wake is not None:
            self._timer = asyncio.create_task(self._alarm(wake))

    async def _alarm(self, wake_ms: int) -> None:
        await self.clock.sleep_until(wake_ms)
        self._put("tick")

    # ----- connections -----

    async def _on_connect(self, item: _Item) -> None:
        pid, sock = item.player_id or "", item.socket
        assert sock is not None
        old = self.sockets.get(pid)
        if old is not None and old is not sock:
            await self._close(old, CLOSE_REPLACED, "replaced by a newer connection")
        self._stop_pinger(pid)
        self.sockets[pid] = sock
        event: eng.Event = (
            eng.Reconnect(pid)  # a no-op if the seat was never absent (replaced socket)
            if pid in self.state.players
            else eng.Join(pid, item.display_name, item.as_host)
        )
        messages = await self._apply(event, item.at_ms)
        if any(isinstance(m, eng.Error) and m.player_id == pid for m in messages):
            # Refused — a late join, a full lobby, a dropped seat. The
            # error reached the socket through fan-out; now close it.
            if self.sockets.get(pid) is sock:
                del self.sockets[pid]
            await self._close(sock, CLOSE_REFUSED, "not seated in this game")
            return
        await self._send(pid, proto.state_message(self.state, pid, self.texts))
        # A new connection is a new path: measure it afresh.
        self.latency[pid] = latency.RttTracker()
        self._pingers[pid] = asyncio.create_task(self._ping_loop(pid, sock))

    async def _on_disconnect(self, item: _Item) -> None:
        pid = item.player_id or ""
        if self.sockets.get(pid) is not item.socket:
            return  # a replaced or already-dropped socket; nothing changes
        del self.sockets[pid]
        self._stop_pinger(pid)
        if pid in self.state.players:
            await self._apply(eng.Disconnect(pid), item.at_ms)

    # ----- RTT (§5) -----

    async def _ping_loop(self, pid: str, sock: Socket) -> None:
        """Queues a `ping` for this socket: a burst on connect, then one
        every PING_INTERVAL_MS. The game task does the stamping and the
        sending, so the pings interleave with everything else in order."""
        for i in range(latency.PING_BURST):
            if i:
                await self.clock.sleep_until(self.clock.now_ms() + latency.PING_BURST_GAP_MS)
            self._put("ping", player_id=pid, socket=sock)
        while True:
            await self.clock.sleep_until(self.clock.now_ms() + latency.PING_INTERVAL_MS)
            self._put("ping", player_id=pid, socket=sock)

    def _stop_pinger(self, pid: str) -> None:
        task = self._pingers.pop(pid, None)
        if task is not None:
            task.cancel()

    async def _on_ping(self, item: _Item) -> None:
        pid = item.player_id or ""
        if self.sockets.get(pid) is not item.socket:
            return  # queued for a socket that has since gone
        stamp = self.latency[pid].ping(self.clock.now_ms())
        await self._send(pid, proto.PingOut(server_ms=stamp))

    def _rtt_ms(self, player_id: str) -> int:
        """The player's rolling median round trip, 0 until measured. Half
        of it comes off the recorded response time (§5) — telemetry and
        the tiebreak, never a scoring input."""
        tracker = self.latency.get(player_id)
        return tracker.rtt_ms if tracker is not None else 0

    # ----- messages -----

    async def _on_message(self, item: _Item) -> None:
        pid = item.player_id or ""
        if self.sockets.get(pid) is None:
            return
        try:
            msg = proto.parse_client_message(item.raw)
        except (ValidationError, ValueError) as exc:
            await self._send(pid, proto.ErrorOut(code="bad_message", message=_first_error(exc)))
            return
        match msg:
            case proto.SyncIn():
                await self._send(pid, proto.SyncReply(client_ms=msg.client_ms, server_ms=item.at_ms))
            case proto.PongIn():
                # Both ends on the server clock: the ping's stamp and the
                # pong's enqueue stamp. Unknown or stale echoes are dropped.
                if (tracker := self.latency.get(pid)) is not None:
                    tracker.pong(msg.server_ms, item.at_ms)
            case proto.ReportIn():
                self._spawn(self._report(pid, msg))
            case proto.StartIn():
                await self._start(pid, item.at_ms)
            case _:
                event = proto.to_event(msg, pid, rtt_ms=self._rtt_ms(pid))
                assert event is not None
                await self._apply(event, item.at_ms)

    # ----- start -----

    async def _start(self, pid: str, at_ms: int) -> None:
        s = self.state
        if s.phase is not Phase.LOBBY or pid != s.host_id or s.present_count() < s.config.min_players:
            # Let the engine word the refusal (wrong_phase, not_host,
            # not_enough_players) — it checks the same things, in order.
            await self._apply(eng.Start(pid), at_ms)
            return
        async with db.SessionLocal() as session:
            row = await session.get(GameSession, self.session_id)
            if row is None or row.status != "lobby":
                await self._send(pid, proto.ErrorOut(code="wrong_phase", message="the game has already started"))
                return
            try:
                draw = await svc.draw_at_start(session, row, present_count=s.present_count())
            except svc.AlreadyDrawn:
                await self._send(pid, proto.ErrorOut(code="wrong_phase", message="the game has already started"))
                return
            if not any(draw.pools.values()):
                await session.rollback()
                await self._send(pid, proto.ErrorOut(code="no_questions", message="no questions were drawn"))
                return
            row.status = "running"
            row.started_at = datetime.now(timezone.utc)
            await session.commit()
        assert draw.cache is not None
        await svc.cache_questions(await cache.get_redis(), draw.cache)
        self.texts = proto.texts_from_cache(draw.cache)
        s.config = draw.config
        s.pools = {cid: list(qs) for cid, qs in draw.pools.items()}
        s.block_reserve = list(draw.block_reserve)
        self.rng = engine_rng(draw.seed)
        await self._apply(eng.Start(pid), at_ms)

    # ----- end -----

    async def _finish(self, ended: eng.Ended) -> None:
        self.finished = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        try:
            async with db.SessionLocal() as session:
                row = await session.get(GameSession, self.session_id)
                if row is not None:
                    row.status = "finished" if ended.reason == "finished" else "abandoned"
                    row.ended_at = datetime.now(timezone.utc)
                    await session.commit()
        except Exception:
            log.exception("session %s: could not record the end", self.join_code)
        await self.hooks.on_end(self, ended)
        for pid, sock in list(self.sockets.items()):
            self._stop_pinger(pid)
            await self._close(sock, CLOSE_OVER, "game over")
        self.sockets.clear()
        if self._on_finish is not None:
            self._on_finish(self)

    # ----- reports -----

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _report(self, pid: str, msg: proto.ReportIn) -> None:
        """Runs outside the game task (a DB round trip must not hold up a
        round); the ack comes back through the queue."""
        ack = proto.ReportAckOut(question_id=msg.question_id, accepted=False)
        if msg.question_id not in self.shown.get(pid, ()):
            ack.reason = "unknown_question"
        else:
            try:
                async with db.SessionLocal() as session:
                    user = await session.get(User, uuid.UUID(pid))
                    assert user is not None
                    payload = QuestionReportCreate(
                        reason=msg.reason, note=msg.note, session_id=self.session_id
                    )
                    await reports.report_question(session, uuid.UUID(msg.question_id), user, payload)
                    await session.commit()
                ack.accepted = True
            except reports.AlreadyReported:
                ack.reason = "already_reported"
            except reports.QuestionNotFound:
                ack.reason = "unknown_question"
            except Exception:
                log.exception("session %s: report by %s failed", self.join_code, pid)
                ack.reason = "failed"
        self._put("report_done", player_id=pid, payload=ack)


def _first_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        loc = ".".join(str(x) for x in first.get("loc", ()) if x != "type")
        return f"{loc}: {first['msg']}" if loc else str(first["msg"])
    return "not a JSON message"


# ---------- registry ----------


class Registry:
    """The live sessions of this process, by join code (single-instance
    MVP, §3). A runtime is created on the first socket to a lobby and
    forgotten when its game ends. A session found `running` in the DB
    with no runtime here is not live: `app.game.persistence.resume`
    brings such sessions back from their snapshots at startup, through
    `restore`."""

    def __init__(self, *, clock: Clock | None = None, hooks: RuntimeHooks | None = None) -> None:
        self.clock = clock
        self.hooks = hooks
        self._live: dict[str, SessionRuntime] = {}

    async def restore(
        self, session: GameSession, snap: Snapshot, texts: Mapping[str, proto.QuestionText]
    ) -> SessionRuntime:
        """A runtime for a running session, continued from its snapshot
        (SessionRuntime.resume). Live from here on, unless resuming ended
        the game outright."""
        rt = SessionRuntime(
            session.id,
            session.join_code,
            snap.state.config,
            clock=self.clock,
            hooks=self.hooks,
            on_finish=self._forget,
        )
        self._live[session.join_code] = rt
        try:
            await rt.resume(snap, texts)
        except BaseException:
            self._forget(rt)
            raise
        if not rt.finished:
            rt.start_task()
        return rt

    def get(self, join_code: str) -> SessionRuntime | None:
        return self._live.get(join_code)

    def live_codes(self) -> list[str]:
        return list(self._live)

    async def get_or_load(self, join_code: str, session: GameSession) -> SessionRuntime | None:
        rt = self._live.get(join_code)
        if rt is not None:
            return rt
        if session.status != "lobby":
            return None
        rt = SessionRuntime(
            session.id,
            join_code,
            svc.config_from(session.config_overrides),
            clock=self.clock,
            hooks=self.hooks,
            on_finish=self._forget,
        )
        self._live[join_code] = rt
        rt.start_task()
        return rt

    def _forget(self, rt: SessionRuntime) -> None:
        if self._live.get(rt.join_code) is rt:
            del self._live[rt.join_code]

    async def shutdown(self) -> None:
        for rt in list(self._live.values()):
            await rt.stop()
        self._live.clear()
        if self.hooks is not None:
            await self.hooks.on_shutdown()


registry = Registry()
