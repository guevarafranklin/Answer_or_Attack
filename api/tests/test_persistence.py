"""Phase 2 §10 step 7: the Redis snapshot, the event log, resume after a
restart, and replay through the pure engine.

Games run on the `live` scratch DB with a hand-moved clock and fake
sockets (tests/test_runtime.py), through a registry wired to the real
`Persistence` hooks — the same wiring app.main does — so what lands in
Redis and `session_events` is what production would write.
"""
from __future__ import annotations

import asyncio
import json
import random
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.game import engine as eng
from app.game import persistence, runtime, snapshot
from app.game.engine import Phase
from app.game.replay import ReplayError, replay
from app.game.seed import engine_rng
from app.models import GameSession, QuestionServe, SessionEvent, SessionPlayer
from app.rules import OPTION_COUNT
from app.services import sessions as svc
from tests.asgi_ws import Closed
from tests.test_runtime import FakeSocket, ManualClock, World, live, settled  # noqa: F401 - fixture

NAMES = ("host", "beth", "carl")


# ---------- a table: a game on the persistence hooks, played by hand ----------


@dataclass
class Table:
    live: World
    clock: ManualClock
    code: str
    session_id: uuid.UUID
    tokens: dict[str, str]  # player id -> player token
    socks: dict[str, FakeSocket]
    rng: random.Random
    rt: runtime.SessionRuntime

    @property
    def registry(self) -> runtime.Registry:
        return runtime.registry

    @property
    def hooks(self) -> persistence.Persistence:
        assert isinstance(runtime.registry.hooks, persistence.Persistence)
        return runtime.registry.hooks

    @property
    def key(self) -> str:
        return persistence.state_key(self.session_id)

    def send(self, pid: str, message: dict[str, Any]) -> None:
        self.rt.receive(pid, json.dumps(message))

    async def start(self) -> None:
        self.send(next(iter(self.socks)), {"type": "start"})
        await settled(self.rt)
        assert self.rt.state.phase is Phase.PICK

    def act(self) -> bool:
        """One message from one player that moves the game on, if there is
        one to send: a pick, an answer (right 60% of the time, a little
        server time apart), an attack, a block answer. False when only
        the timer can move it."""
        s, rng = self.rt.state, self.rng
        present = [p for p in s.active_players() if p.present]
        if s.phase is Phase.PICK:
            if s.picker_id is None or not s.players[s.picker_id].present:
                return False
            self.send(s.picker_id, {"type": "pick", "category_id": rng.choice(s.board)})
            return True
        if s.phase is Phase.QUESTION:
            assert s.question is not None
            for p in present:
                if p.id not in s.answers:
                    self.clock.set(self.clock.t + rng.randint(100, 1500))
                    self.send(p.id, {"type": "answer", "question_id": s.question.id, "option": self._option(s.question)})
                    return True
            return False
        if s.phase is Phase.ATTACK:
            for p in present:
                if s.awaiting_attack(p.id):
                    targets = [q.id for q in s.active_players() if q.id != p.id]
                    self.send(p.id, {"type": "attack", "target_player_id": rng.choice(targets)})
                    return True
            return False
        if s.phase is Phase.BLOCK:
            for pid, b in s.blocks.items():
                if b.answer is None and b.question is not None and s.players[pid].present:
                    self.send(pid, {"type": "answer", "question_id": b.question.id, "option": self._option(b.question)})
                    return True
            return False
        return False

    def _option(self, q: eng.Question) -> int:
        return q.correct_option if self.rng.random() < 0.6 else (q.correct_option + 1) % OPTION_COUNT

    async def play_until(self, done: Callable[[runtime.SessionRuntime], bool]) -> None:
        """Act until `done` (or the end); when no one has anything to do,
        move the clock to the runtime's next deadline."""
        rt = self.rt
        while not rt.finished and not done(rt):
            if self.act():
                await settled(rt)
                continue
            wake = rt._next_wake_ms()
            assert wake is not None, "stuck: nothing to do and no timer"
            self.clock.set(wake)
            await settled(rt)

    async def play_to_end(self) -> None:
        await self.play_until(lambda rt: False)
        assert self.rt.finished and self.rt.state.phase is Phase.END

    async def reconnect_all(self) -> None:
        for pid in self.socks:
            self.socks[pid] = FakeSocket()
            self.rt.connect(pid, pid[:4], self.socks[pid], as_host=False)
        await settled(self.rt)

    async def row(self) -> GameSession:
        return await self.live.session(self.code)

    async def events(self) -> list[tuple[int, int, str, dict[str, Any]]]:
        async with self.live.factory() as s:
            rows = await s.execute(
                select(SessionEvent.seq, SessionEvent.at_ms, SessionEvent.kind, SessionEvent.payload)
                .where(SessionEvent.session_id == self.session_id)
                .order_by(SessionEvent.seq)
            )
            return [tuple(r) for r in rows]  # type: ignore[misc]

    async def event_count(self) -> int:
        async with self.live.factory() as s:
            n = await s.scalar(select(func.count()).select_from(SessionEvent).where(SessionEvent.session_id == self.session_id))
            return n or 0

    def stored(self) -> snapshot.Snapshot:
        raw, ttl = self.live.redis.store[self.key]
        assert ttl == timedelta(hours=3)
        return snapshot.load(raw)


def _install(live: World, clock: ManualClock, hooks: persistence.Persistence) -> runtime.Registry:
    """A registry on the hooks, where the app and the tests look for it."""
    reg = runtime.Registry(clock=clock, hooks=hooks)
    runtime.registry = reg  # the ws router reads the module attribute at call time
    live.registry = reg
    return reg


@pytest_asyncio.fixture(autouse=True)
async def _own_registry(live: World):
    """A registry made here is shut down at the end of the test, and the
    module attribute goes back to the fixture's (which its teardown stops)."""
    original = runtime.registry
    yield
    if runtime.registry is not original:
        await runtime.registry.shutdown()
        runtime.registry = original


async def table(
    live: World, *, seed: int = 1, hooks: persistence.Persistence | None = None, **overrides: Any
) -> Table:
    clock = ManualClock()
    reg = _install(live, clock, hooks or persistence.Persistence())
    cat_a, cat_b = await live.bank(), await live.bank()
    users = [await live.user(n) for n in NAMES]
    code = await live.lobby(users[0], cat_a, cat_b, **overrides)
    session = await live.session(code)
    rt = await reg.get_or_load(code, session)
    assert rt is not None
    tokens, socks = {}, {}
    for u, name in zip(users, NAMES):
        _, tokens[str(u)] = await live.seat(u, code, name)
        socks[str(u)] = FakeSocket()
        rt.connect(str(u), name, socks[str(u)], as_host=u == users[0])
    await settled(rt)
    return Table(live, clock, code, session.id, tokens, socks, random.Random(seed), rt)


async def _replay_prefix(t: Table, events: list[tuple[int, int, str, dict[str, Any]]]) -> eng.GameState:
    """The pure engine over a slice of the log, from the drawn start —
    `replay` for a prefix."""
    async with t.live.factory() as s:
        row = await s.get(GameSession, t.session_id)
        assert row is not None
        draw = await svc.load_draw(s, row)
    state, rng = eng.new_game(draw.config, draw.pools, draw.block_reserve), engine_rng(draw.seed)
    for _, at_ms, kind, payload in events:
        state, _ = eng.step(state, snapshot.decode_event(kind, payload), at_ms, rng)
    return state


def _contiguous(events: list[tuple[int, int, str, dict[str, Any]]]) -> bool:
    return [e[0] for e in events] == list(range(1, len(events) + 1))


# ---------- the snapshot and the log while a game runs ----------


@pytest.mark.asyncio
async def test_every_step_is_logged_with_its_stamp_and_the_snapshot_follows(live: World):
    t = await table(live)
    await t.start()
    await t.play_until(lambda rt: rt.state.round == 2)
    rt = t.rt
    await t.hooks.flush(rt)

    events = await t.events()
    assert _contiguous(events) and len(events) == rt.seq
    kinds = [e[2] for e in events]
    assert kinds[:4] == ["join", "join", "join", "start"]
    assert "pick" in kinds and "answer" in kinds and "tick" in kinds
    stamps = [e[1] for e in events]
    assert stamps == sorted(stamps) and stamps[-1] <= t.clock.t
    # The stamp is the enqueue time, not the apply time: the answers were
    # sent a little apart on the clock.
    answer_stamps = [e[1] for e in events if e[2] == "answer"]
    assert len(set(answer_stamps)) == len(answer_stamps)

    snap = t.stored()
    assert snap.seq == rt.seq and snap.state == rt.state
    assert snap.rng.getstate() == rt.rng.getstate()
    assert snap.shown == rt.shown and snap.saved_ms <= t.clock.t
    await t.registry.shutdown()


@pytest.mark.asyncio
async def test_snapshot_writes_are_coalesced(live: World):
    t = await table(live)
    sets: list[str] = []
    original_set = live.redis.set

    async def counting_set(key: str, value: str, ex: timedelta | None = None) -> None:
        sets.append(key)
        await original_set(key, value, ex)

    live.redis.set = counting_set  # type: ignore[method-assign]
    await t.start()
    before = len(sets)
    # A burst: three answers enqueued together, applied in one go.
    s = t.rt.state
    t.send(s.picker_id or "", {"type": "pick", "category_id": s.board[0]})
    await settled(t.rt)
    qid = t.rt.state.question.id  # type: ignore[union-attr]
    for pid in t.socks:
        t.send(pid, {"type": "answer", "question_id": qid, "option": 0})
    await settled(t.rt)
    await t.hooks.flush(t.rt)
    burst_writes = len([k for k in sets[before:] if k == t.key])
    assert 1 <= burst_writes < 4  # four steps (pick + 3 answers), fewer snapshot writes
    assert t.stored().seq == t.rt.seq  # and the last one is the latest state
    await t.registry.shutdown()


# ---------- resume ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_snapshot", [False, True])
async def test_killed_mid_question_the_game_resumes_and_finishes_as_its_events_say(live: World, stale_snapshot):
    """Stop the process with one answer in on the second question; bring
    it back; everyone reconnects; play to the end. The event log then
    replays to exactly the state the live game ended in.

    With `stale_snapshot`, the snapshot in Redis is older than the log
    (the writer got the events in but not the state): the game resumes
    from the snapshot *plus* the logged events past it, so the answer
    acknowledged just before the crash still counts, and the resumed
    state is what replaying the whole log gives."""
    t = await table(live, seed=7)
    await t.start()
    await t.play_until(lambda rt: rt.state.round == 2 and rt.state.phase is Phase.PICK)
    await t.hooks.flush(t.rt)
    older = live.redis.store[t.key]
    await t.play_until(lambda rt: rt.state.round == 2 and rt.state.phase is Phase.QUESTION and len(rt.state.answers) == 1)
    killed = t.rt
    killed_state = killed.state
    (answered,) = killed_state.answers
    ack = t.socks[answered].of("answer_ack")[-1]
    assert ack["accepted"] and ack["question_id"] == killed_state.question.id  # type: ignore[union-attr]

    await t.registry.shutdown()  # flushes the writers
    assert (await t.row()).status == "running"
    events_before = await t.events()
    assert len(events_before) == killed.seq and _contiguous(events_before)
    snap = t.stored()
    assert snap.seq == killed.seq and snap.state == killed_state
    if stale_snapshot:
        live.redis.store[t.key] = older
        snap = t.stored()
        assert snap.seq < killed.seq and snap.state.phase is Phase.PICK

    t.clock.set(t.clock.t + 2_000)
    _install(live, t.clock, persistence.Persistence())
    restored = await persistence.resume(t.registry)
    assert [rt.join_code for rt in restored] == [t.code]
    rt = t.registry.get(t.code)
    assert rt is not None
    t.rt = rt
    resumed_at = t.clock.t

    # The game is back where the log left it — the snapshot brought up
    # to the last logged event — plus one Disconnect per player: all
    # absent, rejoin window from now.
    assert rt.seq == killed.seq + 3
    assert rt.state.phase is Phase.QUESTION and rt.state.round == 2
    assert rt.state.answers == killed_state.answers  # the acked answer counts
    assert rt.state.question == killed_state.question
    assert rt.rng.getstate() == killed.rng.getstate()
    for p in rt.state.players.values():
        assert not p.present and not p.dropped and p.absent_since_ms == resumed_at
    assert rt.state.low_presence_since_ms == resumed_at
    # Timers: the stored phase deadline is still ahead and is the next wake.
    assert rt.state.phase_end_ms == killed_state.phase_end_ms and rt.state.phase_end_ms > resumed_at
    assert rt._armed_for == rt._next_wake_ms() == min(rt.state.phase_end_ms, resumed_at + 20_000)
    assert rt.texts and all(q.id in rt.texts for pool in rt.state.pools.values() for q in pool)
    assert rt.shown == killed.shown  # the question shown after the stale snapshot is known
    # The log is untouched and continues from where it was.
    await t.hooks.flush(rt)
    events = await t.events()
    assert _contiguous(events) and len(events) == rt.seq
    assert events[: killed.seq] == events_before
    assert [e[2] for e in events[killed.seq :]] == ["disconnect"] * 3
    # What the players see on return is what the whole log says.
    async with live.factory() as s:
        assert await replay(s, t.session_id) == rt.state

    await t.reconnect_all()
    assert all(p.present for p in rt.state.players.values())
    assert rt.state.low_presence_since_ms is None
    await t.play_to_end()
    assert rt.state.end_reason == "finished" and rt.state.results is not None

    row = await t.row()
    assert row.status == "finished" and row.ended_at is not None
    assert t.key not in live.redis.store  # a finished game keeps no snapshot
    events = await t.events()
    assert _contiguous(events) and len(events) == rt.seq

    async with live.factory() as s:
        final = await replay(s, t.session_id)
    assert final == rt.state
    # The snapshot the game came back from is the log's own state at that seq.
    assert await _replay_prefix(t, events[: snap.seq]) == snap.state


@pytest.mark.asyncio
async def test_a_log_that_ended_the_game_finishes_it_at_resume(live: World):
    """The process died after the final step but before the end reached
    the DB: the snapshot is a round behind, the log holds the end."""
    t = await table(live, seed=5)
    await t.start()
    await t.play_until(lambda rt: rt.state.round == 3)
    await t.hooks.flush(t.rt)
    older = live.redis.store[t.key]
    await t.play_to_end()
    final = t.rt.state
    events = await t.events()
    async with live.factory() as s:
        row = await s.get(GameSession, t.session_id)
        assert row is not None
        row.status, row.ended_at = "running", None  # as if _finish never ran
        await s.commit()
    live.redis.store[t.key] = older

    _install(live, t.clock, persistence.Persistence())
    restored = await persistence.resume(t.registry)
    assert len(restored) == 1 and restored[0].finished and restored[0].state == final
    assert t.registry.live_codes() == []
    row = await t.row()
    assert row.status == "finished" and row.ended_at is not None
    assert t.key not in live.redis.store
    assert await t.events() == events  # nothing added: no Disconnects at END


@pytest.mark.asyncio
async def test_rows_past_a_gap_in_the_log_are_dropped_at_resume(live: World):
    t = await table(live, seed=2)
    await t.start()
    await t.play_until(lambda rt: rt.state.round == 2 and rt.state.phase is Phase.PICK)
    await t.hooks.flush(t.rt)
    older = t.stored()
    await t.play_until(lambda rt: rt.state.round == 3)
    await t.registry.shutdown()
    events_before = await t.events()
    gap = older.seq + 2
    async with live.factory() as s:
        victim = await s.scalar(select(SessionEvent).where(SessionEvent.session_id == t.session_id, SessionEvent.seq == gap))
        assert victim is not None
        await s.delete(victim)
        await s.commit()
    live.redis.store[t.key] = (snapshot.dump(older.state, older.rng, older.seq, older.shown, older.saved_ms), timedelta(hours=3))

    _install(live, t.clock, persistence.Persistence())
    (rt,) = await persistence.resume(t.registry)
    t.rt = rt
    assert rt.seq == older.seq + 1 + 3  # the one event before the gap, then the Disconnects
    await t.hooks.flush(rt)
    events = await t.events()
    assert _contiguous(events) and events[: older.seq + 1] == events_before[: older.seq + 1]
    async with live.factory() as s:
        assert await replay(s, t.session_id) == rt.state


@pytest.mark.asyncio
async def test_replay_of_a_finished_game_equals_its_live_final_state(live: World):
    t = await table(live, seed=3)
    await t.start()
    await t.play_to_end()
    rt = t.rt
    assert rt.state.results is not None and len(rt.state.serves) > 0
    async with live.factory() as s:
        final = await replay(s, t.session_id)
    assert final == rt.state
    assert final.results == rt.state.results and final.serves == rt.state.serves
    assert (await t.row()).status == "finished"
    assert t.key not in live.redis.store
    assert t.registry.get(t.code) is None


@pytest.mark.asyncio
async def test_replay_refuses_a_gap_in_the_log_and_unknown_sessions(live: World):
    t = await table(live)
    await t.start()
    await t.play_until(lambda rt: rt.state.round == 2)
    await t.hooks.flush(t.rt)
    async with live.factory() as s:
        row = await s.scalar(select(SessionEvent).where(SessionEvent.session_id == t.session_id, SessionEvent.seq == 3))
        assert row is not None
        await s.delete(row)
        await s.commit()
    async with live.factory() as s:
        with pytest.raises(ReplayError, match="jumps from seq 2 to 4"):
            await replay(s, t.session_id)
        with pytest.raises(ReplayError, match="no session"):
            await replay(s, uuid.uuid4())
    await t.registry.shutdown()


@pytest.mark.asyncio
async def test_a_running_session_with_no_snapshot_is_abandoned(live: World):
    t = await table(live)
    await t.start()
    await t.play_until(lambda rt: rt.state.phase is Phase.QUESTION)
    await t.registry.shutdown()
    assert (await t.row()).status == "running"
    del live.redis.store[t.key]

    _install(live, t.clock, persistence.Persistence())
    assert await persistence.resume(t.registry) == []
    assert t.registry.live_codes() == []
    row = await t.row()
    assert row.status == "abandoned" and row.ended_at is not None
    # Not live: a returning player is told so at the handshake.
    pid, token = next(iter(t.tokens.items()))
    with pytest.raises(Closed) as exc:
        await live.connect(t.code, token)
    assert exc.value.code == runtime.CLOSE_OVER
    # A second boot finds nothing to do.
    assert await persistence.resume(t.registry) == []


@pytest.mark.asyncio
async def test_nothing_running_means_nothing_touched(live: World):
    t = await table(live)  # a lobby, not running
    _install(live, t.clock, persistence.Persistence())
    assert await persistence.resume(t.registry) == []
    assert (await t.row()).status == "lobby"


@pytest.mark.asyncio
async def test_redis_down_at_boot_leaves_running_sessions_for_the_next_boot(live: World):
    t = await table(live)
    await t.start()
    await t.registry.shutdown()
    live.redis.down = True
    _install(live, t.clock, persistence.Persistence())
    assert await persistence.resume(t.registry) == []
    assert (await t.row()).status == "running"
    live.redis.down = False
    assert len(await persistence.resume(t.registry)) == 1
    await t.registry.shutdown()


# ---------- the writer never blocks the queue ----------


class GatedFactory:
    """A session factory whose sessions cannot execute anything until
    the gate opens: a DB that has stopped answering."""

    def __init__(self, inner: Callable[[], Any]) -> None:
        self.inner = inner
        self.gate = asyncio.Event()
        self.waiting = 0

    def __call__(self) -> _GatedSession:
        return _GatedSession(self)


class _GatedSession:
    def __init__(self, factory: GatedFactory) -> None:
        self.factory = factory
        self.session: Any = None

    async def __aenter__(self) -> _GatedSession:
        self.session = await self.factory.inner().__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.session.__aexit__(*exc)

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.factory.waiting += 1
        try:
            await self.factory.gate.wait()
        finally:
            self.factory.waiting -= 1
        return await self.session.execute(*args, **kwargs)

    async def commit(self) -> None:
        await self.session.commit()


@pytest.mark.asyncio
async def test_a_stalled_db_never_holds_up_a_round(live: World):
    gated = GatedFactory(live.factory)
    t = await table(live, hooks=persistence.Persistence(session_factory=gated))
    await t.start()
    # A full round and more with the DB stuck: the game does not notice.
    await t.play_until(lambda rt: rt.state.round == 3)
    rt = t.rt
    assert rt.state.round == 3 and rt.seq > 12
    assert rt.queue.empty()
    assert gated.waiting == 1  # the writer, stuck on its first insert
    assert await t.event_count() == 0
    assert t.key not in live.redis.store  # events land before the snapshot
    writer = t.hooks._writers[t.session_id]
    assert writer.pending and writer.task is not None and not writer.task.done()

    # Shutdown flushes: it waits for the writer, which waits for the DB.
    stopping = asyncio.create_task(t.registry.shutdown())
    await asyncio.sleep(0.05)
    assert not stopping.done()
    gated.gate.set()
    await asyncio.wait_for(stopping, 5)
    events = await t.events()
    assert _contiguous(events) and len(events) == rt.seq
    assert t.stored().seq == rt.seq  # the latest state, once


@pytest.mark.asyncio
async def test_the_end_of_a_game_flushes_the_log_and_drops_the_snapshot(live: World):
    gated = GatedFactory(live.factory)
    gated.gate.set()
    t = await table(live, hooks=persistence.Persistence(session_factory=gated))
    await t.start()
    await t.play_until(lambda rt: rt.state.round == 2)
    gated.gate.clear()
    await t.play_until(lambda rt: rt.state.round == 3)
    assert await t.event_count() < t.rt.seq
    # Let the game end with the DB slow: on_end waits for the writer.
    ending = asyncio.create_task(t.play_to_end())
    await asyncio.sleep(0.05)
    gated.gate.set()
    await asyncio.wait_for(ending, 10)
    assert len(await t.events()) == t.rt.seq
    assert t.key not in live.redis.store
    assert t.hooks._writers == {}


# ---------- §10 step 8: persistence at END ----------


async def _persisted(t: Table) -> tuple[list[SessionPlayer], list[QuestionServe]]:
    async with live_factory(t)() as s:
        seats = (await s.scalars(select(SessionPlayer).where(SessionPlayer.session_id == t.session_id))).all()
        serves = (await s.scalars(select(QuestionServe).where(QuestionServe.session_id == t.session_id))).all()
    return list(seats), list(serves)


def live_factory(t: Table):
    return t.live.factory


def _serve_key(x: Any) -> tuple[str, str, str, int | None]:
    return (str(x.question_id), str(x.user_id if isinstance(x, QuestionServe) else x.player_id), x.outcome, x.response_ms)


def _assert_end_persisted(t: Table, state: eng.GameState, seats: list[SessionPlayer], serves: list[QuestionServe]) -> None:
    assert state.phase is Phase.END and state.results is not None
    by_user = {str(seat.user_id): seat for seat in seats}
    for p in state.players.values():
        seat = by_user[p.id]
        assert (seat.starting_xp, seat.final_xp, seat.delta_xp) == (p.starting_xp, p.xp, p.xp - p.starting_xp)
    for r in state.results:
        assert (by_user[r.player_id].final_xp, by_user[r.player_id].delta_xp) == (r.final_xp, r.delta)
    assert sorted(map(_serve_key, serves)) == sorted(map(_serve_key, state.serves))
    assert all(x.locale == "en" and x.served_at is not None for x in serves)
    assert all(x.response_ms is None or x.response_ms >= 0 for x in serves)


@pytest.mark.asyncio
async def test_a_finished_game_persists_seats_and_serves_from_the_final_state(live: World):
    t = await table(live, seed=9)
    await t.start()
    await t.play_to_end()
    state = t.rt.state
    seats, serves = await _persisted(t)
    _assert_end_persisted(t, state, seats, serves)
    row = await t.row()
    assert row.status == "finished" and row.ended_at is not None

    # One serve per question shown per player: three round questions to
    # three players, plus one per block question asked.
    round_qs = {x.question_id for x in state.serves if x.kind == "question"}
    blocks = [x for x in state.serves if x.kind == "block"]
    assert len(round_qs) == 3 and len(blocks) > 0
    assert len(serves) == len(round_qs) * 3 + len(blocks) == len(state.serves)
    outcomes = {x.outcome for x in serves}
    assert outcomes <= {"correct", "incorrect", "timeout", "absent"} and {"correct", "incorrect"} <= outcomes
    # Response times are the engine's judged ones (RTT credit applied),
    # only for answered serves.
    for x in serves:
        assert (x.response_ms is None) == (x.outcome in ("timeout", "absent"))


@pytest.mark.asyncio
async def test_a_retried_or_replayed_end_writes_nothing_twice(live: World):
    t = await table(live, seed=9)
    await t.start()
    await t.play_to_end()
    rt = t.rt
    seats, serves = await _persisted(t)
    ended_at = (await t.row()).ended_at

    # The hook again (a retry), and the function on a replayed final state.
    await t.hooks.on_end(rt, eng.ended(rt.state))
    async with live.factory() as s:
        replayed = await replay(s, t.session_id)
    assert await persistence.persist_end(live.factory, t.session_id, replayed) is False

    again_seats, again_serves = await _persisted(t)
    assert [(x.user_id, x.starting_xp, x.final_xp, x.delta_xp) for x in again_seats] == [
        (x.user_id, x.starting_xp, x.final_xp, x.delta_xp) for x in seats
    ]
    assert sorted(x.id for x in again_serves) == sorted(x.id for x in serves)
    assert (await t.row()).ended_at == ended_at
    with pytest.raises(persistence.NotEnded):
        await persistence.persist_end(live.factory, t.session_id, eng.new_game(rt.state.config, {}, []))


@pytest.mark.asyncio
async def test_resuming_an_already_ended_log_does_not_persist_again(live: World):
    """The crash-after-the-final-step case: the first process may or may
    not have persisted before dying; resume finishes the game and the
    result is there exactly once either way."""
    t = await table(live, seed=5)
    await t.start()
    await t.play_until(lambda rt: rt.state.round == 3)
    await t.hooks.flush(t.rt)
    older = live.redis.store[t.key]
    await t.play_to_end()
    seats, serves = await _persisted(t)
    assert serves and all(x.final_xp is not None for x in seats)
    async with live.factory() as s:
        row = await s.get(GameSession, t.session_id)
        assert row is not None
        row.status, row.ended_at = "running", None
        await s.commit()
    live.redis.store[t.key] = older

    _install(live, t.clock, persistence.Persistence())
    (rt,) = await persistence.resume(t.registry)
    assert rt.finished
    again_seats, again_serves = await _persisted(t)
    assert sorted(x.id for x in again_serves) == sorted(x.id for x in serves)
    assert [(x.user_id, x.final_xp) for x in again_seats] == [(x.user_id, x.final_xp) for x in seats]
    row = await t.row()
    assert row.status == "finished" and row.ended_at is not None


@pytest.mark.asyncio
async def test_an_abandoned_game_persists_what_was_played_dropped_player_included(live: World):
    t = await table(live, seed=4, question_count=5, rejoin_seconds=3, abandon_seconds=5)
    await t.start()
    host, beth, carl = t.socks
    # Carl leaves during the first question, is absent for it, and is
    # dropped once the rejoin window passes.
    await t.play_until(lambda rt: rt.state.phase is Phase.QUESTION)
    t.rt.disconnect(carl, t.socks[carl])
    await settled(t.rt)
    assert not t.rt.state.players[carl].present
    await t.play_until(lambda rt: rt.state.players[carl].dropped)
    assert t.rt.state.phase is not Phase.END
    # The other two play on into round 3, then both leave: abandoned
    # after abandon_seconds with nobody back.
    await t.play_until(lambda rt: rt.state.round == 3 and rt.state.phase is Phase.QUESTION)
    for pid in (host, beth):
        t.rt.disconnect(pid, t.socks[pid])
    await settled(t.rt)
    await t.play_to_end()
    state = t.rt.state
    assert state.end_reason == "abandoned"

    row = await t.row()
    assert row.status == "abandoned" and row.ended_at is not None
    seats, serves = await _persisted(t)
    _assert_end_persisted(t, state, seats, serves)
    by_user = {str(x.user_id): x for x in seats}
    carl_seat = by_user[carl]
    assert carl_seat.starting_xp is not None and carl_seat.final_xp == carl_seat.starting_xp and carl_seat.delta_xp == 0
    carl_serves = [x for x in serves if str(x.user_id) == carl]
    assert carl_serves and all(x.outcome == "absent" and x.response_ms is None for x in carl_serves)
    # Only what was played: the two round questions that reached their
    # reveal (the third was open when the game died), not five.
    assert len({x.question_id for x in state.serves if x.kind == "question"}) == 2
    assert len(serves) == len(state.serves) < 5 * 3
    assert t.key not in live.redis.store


@pytest.mark.asyncio
async def test_a_seat_that_never_played_keeps_nulls(live: World):
    t = await table(live, seed=9)
    dan = await live.user("dan")
    await live.seat(dan, t.code, "dan")  # seated, never connected
    await t.start()
    await t.play_to_end()
    seats, serves = await _persisted(t)
    dan_seat = next(x for x in seats if x.user_id == dan)
    assert (dan_seat.starting_xp, dan_seat.final_xp, dan_seat.delta_xp) == (None, None, None)
    assert not [x for x in serves if x.user_id == dan]
    assert sum(x.final_xp is not None for x in seats) == 3
