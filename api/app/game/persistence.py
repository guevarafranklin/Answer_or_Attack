"""The Redis snapshot and the event log (§3, §6), and resuming from them.

`Persistence` plugs into `RuntimeHooks`. Both hooks only note work and
return; one background writer per live session does the I/O, so a slow
store never holds up a round:

* the **snapshot** — the full engine state, the rng's state, the event
  counter (`app.game.snapshot`) — goes to `session:{id}:state` with a
  3 h TTL. Writes are coalesced: the writer takes the *latest* state
  when it gets round to it, so a burst of steps costs one write, not
  one per step;
* the **event log** — every applied event with its enqueue stamp — is
  appended to `session_events` in batches. The writer inserts every
  event the snapshot has seen *before* it stores that snapshot, so the
  log is never behind a snapshot: on resume, the log may run past the
  snapshot (steps the players saw acknowledged whose state write never
  happened), never short of it, and the surplus is applied to the
  snapshot through the pure engine — exactly as a replay would — so
  nothing a player was told survives only in their memory.

Both are flushed when the game ends and at process shutdown, and the
snapshot of a finished game is deleted.

`resume` runs at startup: every session the DB says is `running` gets
its runtime back from its snapshot (`Registry.restore`), or is marked
`abandoned` when there is none — the socket handshake refuses a running
session with no runtime, so nothing is left half-alive.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache, db
from app.game import engine as eng
from app.game import protocol as proto
from app.game import snapshot
from app.game.engine import GameState
from app.game.runtime import Registry, RuntimeHooks, SessionRuntime
from app.models import GameSession, SessionEvent
from app.services import sessions as svc

log = logging.getLogger(__name__)

SNAPSHOT_TTL = timedelta(hours=3)
INSERT_CHUNK = 500  # events per INSERT
RETRIES = 5  # attempts at one chunk before it is dropped (logged as an error)
RETRY_SECONDS = 1.0

SessionFactory = Callable[[], AsyncSession]


def state_key(session_id: uuid.UUID) -> str:
    return f"session:{session_id}:state"


class _Writer:
    """The background writer of one live session. `wake` starts it if it
    is idle; it runs until nothing is pending and then ends, so an idle
    game holds no task."""

    def __init__(self, rt: SessionRuntime, factory: Callable[[], SessionFactory]) -> None:
        self.rt = rt
        self._factory = factory
        self.pending: list[dict[str, Any]] = []  # session_events rows not yet inserted
        self.dirty = False  # the state has changed since the last snapshot
        self.task: asyncio.Task[None] | None = None

    def wake(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name=f"persist:{self.rt.join_code}")

    async def drain(self) -> None:
        """Everything noted so far is in the stores (or given up on)."""
        while self.pending or self.dirty or (self.task is not None and not self.task.done()):
            if self.task is None or self.task.done():
                self.wake()
            assert self.task is not None
            await self.task

    async def _run(self) -> None:
        try:
            while self.pending or self.dirty:
                snap = None
                if self.dirty:
                    self.dirty = False
                    rt = self.rt
                    snap = snapshot.dump(rt.state, rt.rng, rt.seq, rt.shown, rt.clock.now_ms())
                # Every event this snapshot has seen is pending already
                # (on_event runs before on_state): they land first.
                while self.pending:
                    chunk, self.pending = self.pending[:INSERT_CHUNK], self.pending[INSERT_CHUNK:]
                    await self._insert(chunk)
                if snap is not None:
                    await self._store(snap)
        except Exception:  # pragma: no cover - the writer must never die noisily
            log.exception("session %s: persistence writer failed", self.rt.join_code)

    async def _insert(self, rows: list[dict[str, Any]]) -> None:
        for attempt in range(1, RETRIES + 1):
            try:
                async with self._factory()() as session:
                    await session.execute(insert(SessionEvent), rows)
                    await session.commit()
                return
            except Exception:
                log.warning(
                    "session %s: event log insert failed (attempt %d/%d)",
                    self.rt.join_code, attempt, RETRIES, exc_info=True,
                )
                await asyncio.sleep(RETRY_SECONDS * attempt)
        log.error(
            "session %s: dropping %d events (seq %d–%d) from the log",
            self.rt.join_code, len(rows), rows[0]["seq"], rows[-1]["seq"],
        )

    async def _store(self, snap: str) -> None:
        try:
            redis = await cache.get_redis()
            await redis.set(state_key(self.rt.session_id), snap, ex=SNAPSHOT_TTL)
        except Exception:
            log.warning("session %s: could not store the snapshot", self.rt.join_code, exc_info=True)


class Persistence(RuntimeHooks):
    """The hooks. `session_factory` defaults to `db.SessionLocal`, looked
    up at call time so tests can point it elsewhere."""

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._session_factory = session_factory
        self._writers: dict[uuid.UUID, _Writer] = {}

    def session_factory(self) -> SessionFactory:
        return self._session_factory or db.SessionLocal

    def _writer(self, rt: SessionRuntime) -> _Writer:
        w = self._writers.get(rt.session_id)
        if w is None or w.rt is not rt:  # a resumed runtime takes over from its predecessor
            w = self._writers[rt.session_id] = _Writer(rt, self.session_factory)
        return w

    async def on_event(
        self, rt: SessionRuntime, seq: int, at_ms: int, event: eng.Event, messages: list[eng.Message]
    ) -> None:
        kind, payload = snapshot.encode_event(event)
        self._writer(rt).pending.append(
            {"session_id": rt.session_id, "seq": seq, "at_ms": at_ms, "kind": kind, "payload": payload}
        )

    async def on_state(self, rt: SessionRuntime, state: GameState) -> None:
        w = self._writer(rt)
        w.dirty = True
        w.wake()

    async def on_end(self, rt: SessionRuntime, ended: eng.Ended) -> None:
        w = self._writers.pop(rt.session_id, None)
        if w is not None:
            await w.drain()
        try:
            await (await cache.get_redis()).delete(state_key(rt.session_id))
        except Exception:
            log.warning("session %s: could not delete the snapshot", rt.join_code, exc_info=True)

    async def on_shutdown(self) -> None:
        writers, self._writers = list(self._writers.values()), {}
        await asyncio.gather(*(w.drain() for w in writers))

    async def flush(self, rt: SessionRuntime) -> None:
        """Wait for this session's pending writes (tests, diagnostics)."""
        w = self._writers.get(rt.session_id)
        if w is not None:
            await w.drain()


# ---------- resume ----------


async def resume(registry: Registry, *, session_factory: SessionFactory | None = None) -> list[SessionRuntime]:
    """At startup: bring back every `running` session that has a
    snapshot; mark the rest `abandoned`. Redis being unreachable leaves
    them all as they are (logged): they are not live, and the next boot
    tries again."""
    factory = session_factory or db.SessionLocal
    now = datetime.now(timezone.utc)
    found: list[tuple[GameSession, snapshot.Snapshot, dict[str, proto.QuestionText]]] = []
    async with factory() as session:
        rows = (await session.scalars(select(GameSession).where(GameSession.status == "running"))).all()
        if not rows:
            return []
        try:
            redis = await cache.get_redis()
            raws = [await redis.get(state_key(row.id)) for row in rows]
        except Exception:
            log.exception("cannot reach redis: %d running session(s) not resumed", len(rows))
            return []
        for row, raw in zip(rows, raws):
            if raw is None:
                log.warning("session %s: no snapshot, abandoning", row.join_code)
                _abandon(row, now)
                continue
            try:
                snap = snapshot.load(raw)
                draw = await svc.load_draw(session, row)
            except Exception:
                log.exception("session %s: unusable snapshot, abandoning", row.join_code)
                _abandon(row, now)
                continue
            assert draw.cache is not None
            texts = proto.texts_from_cache(draw.cache)
            tail = (
                await session.execute(
                    select(SessionEvent.seq, SessionEvent.at_ms, SessionEvent.kind, SessionEvent.payload)
                    .where(SessionEvent.session_id == row.id, SessionEvent.seq > snap.seq)
                    .order_by(SessionEvent.seq)
                )
            ).all()
            unapplied = _catch_up(snap, tail, texts)
            if unapplied:
                # A gap in the log (a chunk the writer gave up on): what
                # lies past it cannot be reconciled with the game that
                # continues from the snapshot, and would collide with its
                # seqs. Dropped, loudly.
                log.error(
                    "session %s: %d logged event(s) past a gap after seq %d dropped at resume",
                    row.join_code, unapplied, snap.seq,
                )
                await session.execute(
                    delete(SessionEvent).where(SessionEvent.session_id == row.id, SessionEvent.seq > snap.seq)
                )
            found.append((row, snap, texts))
        await session.commit()

    restored: list[SessionRuntime] = []
    for row, snap, texts in found:
        try:
            restored.append(await registry.restore(row, snap, texts))
            log.info("session %s: resumed at seq %d in %s", row.join_code, snap.seq, snap.state.phase)
        except Exception:
            log.exception("session %s: resume failed, abandoning", row.join_code)
            async with factory() as session:
                fresh = await session.get(GameSession, row.id)
                if fresh is not None:
                    _abandon(fresh, now)
                    await session.commit()
    return restored


def _catch_up(
    snap: snapshot.Snapshot, tail: list[Any], texts: Mapping[str, proto.QuestionText]
) -> int:
    """Apply the events logged past the snapshot to it, in order, through
    the pure engine — the replay of the moments between the last state
    write and the crash. `shown` follows the questions those steps put
    in front of players. Returns how many rows could not be applied
    because the log jumps (0 when it is contiguous)."""
    for i, (seq, at_ms, kind, payload) in enumerate(tail):
        if seq != snap.seq + 1:
            return len(tail) - i
        event = snapshot.decode_event(kind, payload)
        snap.state, messages = eng.step(snap.state, event, at_ms, snap.rng, copy_state=False)
        snap.seq = seq
        for pid, msgs in proto.fan_out(snap.state, messages, texts).items():
            for m in msgs:
                if isinstance(m, (proto.QuestionOut, proto.BlockQuestionOut)):
                    snap.shown.setdefault(pid, set()).add(m.question_id)
    return 0


def _abandon(row: GameSession, now: datetime) -> None:
    row.status = "abandoned"
    row.ended_at = now
