"""Latency and load bots (spec §8): N simulated players per game over real
WebSockets, each with its own accuracy, answer-delay distribution, network
profile and attack policy; `--sessions` games at once.

    cd api && python scripts/bots.py --bots 4 --profile 800
    cd api && python scripts/bots.py --bots 20 --sessions 10 --profile 0 --questions 8
    cd api && python scripts/bots.py --bots 6 --profile 0,150,400,800 --accuracy 0.4,0.9 --attack greedy

Without --base the script starts a second API on the `<DATABASE_URL db>_test`
scratch database (created, migrated and seeded with the six categories and
synthetic live questions if needed — never the dev database) and stops it at
the end. --base drives an API someone else started **on the test DB**; the
dev API on :8000 is refused, because every game here writes sessions, seats
and serves.

What a bot does — the same things the web client does (§5, §7):
- echoes every `ping` as `pong` at once, through its simulated network;
- runs the client clock sync (5 `sync` on connect, lowest-rtt offset, re-sync
  every 30 s) and renders its countdown as `deadline_ms - (now + offset)`;
- simulates its network like the dev client's latency simulator: each message
  in each direction is delayed `base ± jitter` ms (uniform), in order, for the
  profiles 0 (none), 150±50, 400±150 and 800±300;
- answers after a delay drawn from `--delay` ("3500±1500": normal, clipped at
  100 ms; "500-9500": uniform; "4000": fixed), correctly with probability
  `--accuracy`; the delay and accuracy lists cycle over the bots of a game;
- picks a random category when it is the picker, and in an attack window
  either never attacks (passes), attacks a random player, or greedily attacks
  the player with the highest nominal delta (`reveal.deltas`), moving on to
  the next when the target is full.

What the report counts:
- An answer is **on time** when the bot's own countdown was still above zero
  when it tapped. Rejected = `answer_ack {accepted: false}`, an `error`
  (`too_late`, `wrong_phase`, `wrong_question`) for that answer, or a reveal
  that scored the bot `timeout` although it had answered. Rates are per
  network profile, on-time and late separately; the §8 target is fewer than
  2 % of on-time answers rejected at 800±300 with the default grace.
- **Phase-transition lag**, for every transition the server made on its
  timer (the new phase began at or after the old one's `phase_end_ms`):
  `tick lag` = the new phase's start (its `deadline_ms` minus its timer)
  minus the old phase's scheduled end — pure server stamps, the alarm's
  lateness; `arrival lag` = when the `phase` message reached the bot, on the
  server's clock via the bot's offset, minus that same scheduled end — what a
  player's countdown sees, network included. The §8 target (20 bots × 10
  sessions, transitions under 100 ms) is checked on the profile-0 arrival
  lag and the tick lag.
- **Server CPU** from `ps` on the API process (the one this script started,
  or --server-pid): mean over the run and the busiest one-second sample.
- A **game summary**: rounds, duration, outcomes, attacks, blocks, winners
  and tiebreaks from the `end` message of every game.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import statistics
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from websockets.asyncio.client import ClientConnection, connect  # noqa: E402
from websockets.exceptions import ConnectionClosed  # noqa: E402

from dev_api import start_api, wait_http  # noqa: E402

# The dev client's four profiles (spec §7): base ms, jitter ms, per direction.
PROFILES: dict[int, tuple[int, int]] = {0: (0, 0), 150: (150, 50), 400: (400, 150), 800: (800, 300)}
HUMAN_DELAY = "3500±1500"
DEFAULT_GRACE = (200, 400, 1000)  # grace_base_ms, grace_min_ms, grace_max_ms (§2.1)
TIMER_OF = {  # phase -> config field of its timer; input phases also end grace_max later
    "pick": "pick_seconds",
    "question": "question_seconds",
    "reveal": "reveal_seconds",
    "attack": "attack_window_seconds",
    "block": "block_seconds",
}
NO_GRACE = {"reveal"}
SYNC_BURST, SYNC_GAP_S, RESYNC_S = 5, 0.1, 30.0
LATE_CODES = {"too_late", "wrong_phase", "wrong_question"}
DEV_PORTS = {"localhost:8000", "127.0.0.1:8000"}


def now_ms() -> int:
    """The bot's wall clock, like Date.now() in the web client."""
    return int(time.time() * 1000)


# ---------------------------------------------------------------- params ----
def parse_delay(spec: str):
    """A sampler of answer delays in ms: "3500±1500" (normal, clipped at
    100 ms), "500-9500" (uniform) or "4000" (fixed)."""
    s = spec.strip().replace("+-", "±")
    if "±" in s:
        mean, sd = (float(x) for x in s.split("±"))
        return lambda rng: max(100.0, rng.gauss(mean, sd))
    if m := re.fullmatch(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)", s):
        lo, hi = float(m.group(1)), float(m.group(2))
        return lambda rng: rng.uniform(lo, hi)
    fixed = float(s)
    return lambda rng: fixed


def cycle(values: list, i: int):
    return values[i % len(values)]


# ---------------------------------------------------------------- network ----
class Wire:
    """One direction of a simulated network: every item crosses it after
    `base ± jitter` ms, never overtaking the one before (the dev client's
    laggy())."""

    def __init__(self, base: int, jitter: int, rng: random.Random, deliver) -> None:
        self.base, self.jitter, self.rng, self.deliver = base, jitter, rng, deliver
        self.queue: asyncio.Queue[tuple[float, Any]] = asyncio.Queue()
        self.last = 0.0

    def put(self, item: Any) -> None:
        loop = asyncio.get_running_loop()
        delay = max(0.0, self.base + self.rng.uniform(-1, 1) * self.jitter) / 1000
        due = max(loop.time() + delay, self.last)
        self.last = due
        self.queue.put_nowait((due, item))

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            due, item = await self.queue.get()
            if (wait := due - loop.time()) > 0:
                await asyncio.sleep(wait)
            await self.deliver(item)


# ---------------------------------------------------------------- bots ----
@dataclass
class Answer:
    kind: str  # question | block
    question_id: str
    on_time: bool
    countdown_ms: int  # the bot's countdown when it tapped (negative: late)
    accepted: bool | None = None
    how: str | None = None  # ack | error:<code> | timeout_reveal | unanswered


@dataclass
class Transition:
    phase: str
    tick_lag_ms: int
    arrival_lag_ms: int


@dataclass
class Bot:
    game: Game
    name: str
    accuracy: float
    profile: int
    delay: Any  # sampler
    attack: str  # never | random | greedy
    rng: random.Random
    user_id: str = ""
    player_id: str = ""
    token: str = ""
    ws: ClientConnection | None = None
    out: Wire | None = None  # simulated network, outgoing; None at profile 0
    inbox: Wire | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)
    # clock sync (§5)
    offset: float = 0.0
    offset_rtt: float = float("inf")
    offset_at: float = 0.0
    rtts: list[float] = field(default_factory=list)
    # what the bot knows
    config: dict[str, Any] = field(default_factory=dict)
    players: list[str] = field(default_factory=list)
    tokens: int = 0
    deltas: dict[str, int] = field(default_factory=dict)
    open_qid: str | None = None
    prev_phase: tuple[str, int] | None = None  # (phase, scheduled end ms)
    targets: list[str] = field(default_factory=list)
    end: dict[str, Any] | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    # tallies
    answers: list[Answer] = field(default_factory=list)
    pending: dict[str, Answer] = field(default_factory=dict)
    overtaken: int = 0  # the phase moved on before the bot tapped
    transitions: list[Transition] = field(default_factory=list)
    outcomes: Counter = field(default_factory=Counter)
    attacks_made: int = 0
    passes: int = 0
    blocked: int = 0
    not_blocked: int = 0
    errors: Counter = field(default_factory=Counter)

    # ----- wiring -----
    async def connect(self) -> None:
        base, jitter = PROFILES[self.profile]
        url = f"{self.game.run.ws_base}/ws/sessions/{self.game.code}?token={self.token}"
        self.ws = await connect(url, ping_interval=None, max_queue=None)
        self.out = Wire(base, jitter, self.rng, self._raw_send) if base else None
        self.inbox = Wire(base, jitter, self.rng, self.on_message) if base else None
        if self.out:
            self.tasks.append(asyncio.create_task(self.out.run()))
        if self.inbox:
            self.tasks.append(asyncio.create_task(self.inbox.run()))
        self.tasks.append(asyncio.create_task(self._reader()))
        self.tasks.append(asyncio.create_task(self._syncer()))

    async def _raw_send(self, msg: dict[str, Any]) -> None:
        if self.ws is None:
            return
        try:
            await self.ws.send(json.dumps(msg))
        except ConnectionClosed:
            pass

    async def send(self, msg: dict[str, Any]) -> None:
        if self.out:
            self.out.put(msg)
        else:
            await self._raw_send(msg)

    async def _reader(self) -> None:
        assert self.ws is not None
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                if self.inbox:
                    self.inbox.put(m)
                else:
                    await self.on_message(m)
        except ConnectionClosed:
            pass
        finally:
            # The close crosses the simulated network after the data, like
            # the dev client: the server closes right after `end`, and that
            # message must still land first.
            if self.inbox:
                self.inbox.put(None)
            else:
                self.done.set()

    async def _syncer(self) -> None:
        while not self.done.is_set():
            for _ in range(SYNC_BURST):
                await self.send({"type": "sync", "client_ms": now_ms()})
                await asyncio.sleep(SYNC_GAP_S)
            await asyncio.sleep(RESYNC_S)

    def later(self, delay_s: float, coro) -> None:
        async def go() -> None:
            try:
                await asyncio.sleep(delay_s)
            except asyncio.CancelledError:
                coro.close()  # never started: no "coroutine was never awaited" noise
                raise
            await coro

        self.tasks.append(asyncio.create_task(go()))

    async def close(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.ws is not None:
            await self.ws.close()

    # ----- the client clock (§5) -----
    def server_now(self) -> float:
        return now_ms() + self.offset

    def countdown(self, deadline_ms: int) -> int:
        return int(deadline_ms - self.server_now())

    def on_sync_reply(self, m: dict[str, Any]) -> None:
        recv = now_ms()
        rtt = recv - m["client_ms"]
        offset = m["server_ms"] - (m["client_ms"] + recv) / 2
        self.rtts.append(rtt)
        stale = time.monotonic() - self.offset_at > 60
        if rtt <= self.offset_rtt or stale:
            self.offset, self.offset_rtt, self.offset_at = offset, rtt, time.monotonic()

    # ----- messages -----
    async def on_message(self, m: dict[str, Any] | None) -> None:
        if m is None:  # the socket closed
            self.done.set()
            return
        match m["type"]:
            case "ping":
                await self.send({"type": "pong", "server_ms": m["server_ms"]})
            case "sync_reply":
                self.on_sync_reply(m)
            case "lobby" | "state":
                self.players = [p["player_id"] for p in m["players"]]
                self.config = m["config"]
                if m["type"] == "state":
                    if m["you"]:
                        self.tokens = m["you"]["tokens"]
                    if m["end"]:
                        self.finish(m["end"])
                    self.ready.set()
            case "phase":
                self.on_phase(m)
            case "board":
                if m["picker_id"] == self.player_id:
                    self.later(self.rng.uniform(0.3, 1.5), self.pick(m["category_ids"]))
            case "question":
                self.on_question("question", m)
            case "block_question":
                self.on_question("block", m)
            case "answer_ack":
                self.resolve(m["question_id"], m["accepted"], "ack" if m["accepted"] else f"ack:{m['reason']}")
            case "error":
                await self.on_error(m)
            case "reveal":
                self.on_reveal(m)
            case "block_result":
                if m["target_id"] == self.player_id:
                    if m["blocked"]:
                        self.blocked += 1
                    else:
                        self.not_blocked += 1
            case "end":
                self.finish(m)

    def finish(self, end: dict[str, Any]) -> None:
        self.end = end
        for a in self.pending.values():
            a.accepted, a.how = False, "unanswered"
        self.pending.clear()
        self.done.set()

    def on_phase(self, m: dict[str, Any]) -> None:
        phase, deadline = m["phase"], m["deadline_ms"]
        if phase != "question" and phase != "block":
            self.open_qid = None
        timer = TIMER_OF.get(phase)
        if deadline is not None and timer is not None and self.config:
            start = deadline - self.config[timer] * 1000
            if self.prev_phase is not None:
                _, scheduled_end = self.prev_phase
                if start >= scheduled_end - 5:  # the server's timer fired; not an early end
                    self.transitions.append(
                        Transition(phase, start - scheduled_end, int(self.server_now() - scheduled_end))
                    )
            grace = 0 if phase in NO_GRACE else self.config["grace_max_ms"]
            self.prev_phase = (phase, deadline + grace)
        else:
            self.prev_phase = None
        if phase == "attack":
            self.on_attack_window()

    def on_question(self, kind: str, m: dict[str, Any]) -> None:
        qid, deadline = m["question_id"], m["deadline_ms"]
        self.open_qid = qid
        key = self.game.run.answer_key.get(qid)
        correct = m["options"].index(key) if key in m["options"] else None
        if correct is None:
            self.errors["unknown question text"] += 1
        self.later(self.delay(self.rng) / 1000, self.tap(kind, qid, deadline, correct))

    async def tap(self, kind: str, qid: str, deadline: int, correct: int | None) -> None:
        if self.open_qid != qid or self.done.is_set():
            self.overtaken += 1
            return
        if correct is not None and self.rng.random() < self.accuracy:
            option = correct
        else:
            option = self.rng.choice([i for i in range(4) if i != correct])
        left = self.countdown(deadline)
        a = Answer(kind, qid, on_time=left > 0, countdown_ms=left)
        self.answers.append(a)
        self.pending[qid] = a
        await self.send({"type": "answer", "question_id": qid, "option": option})

    def resolve(self, qid: str, accepted: bool, how: str) -> None:
        a = self.pending.pop(qid, None)
        if a is not None:
            a.accepted, a.how = accepted, how

    async def on_error(self, m: dict[str, Any]) -> None:
        code = m["code"]
        if code in LATE_CODES and self.pending:
            qid = next(iter(self.pending))  # the answer still waiting for its verdict
            self.resolve(qid, False, f"error:{code}")
        elif code == "target_full" and self.targets:
            await self.attack_next()
        else:
            self.errors[code] += 1

    def on_reveal(self, m: dict[str, Any]) -> None:
        self.tokens = m["tokens"]
        self.deltas = m["deltas"]
        if m["outcome"] is not None:
            self.outcomes[m["outcome"]] += 1
        if m["outcome"] == "timeout":
            self.resolve(m["question_id"], False, "timeout_reveal")

    async def pick(self, category_ids: list[str]) -> None:
        await self.send({"type": "pick", "category_id": self.rng.choice(category_ids)})

    def on_attack_window(self) -> None:
        if self.tokens < 1:
            return
        window = self.config["attack_window_seconds"]
        delay = self.rng.uniform(0.3, max(0.4, min(2.5, window - 0.5)))
        others = [p for p in self.players if p != self.player_id]
        if self.attack == "never" or not others:
            self.targets = []
        elif self.attack == "greedy":
            self.rng.shuffle(others)
            self.targets = sorted(others, key=lambda p: -self.deltas.get(p, 0))
        else:
            self.rng.shuffle(others)
            self.targets = others[:1]
        self.later(delay, self.attack_next())

    async def attack_next(self) -> None:
        if self.targets:
            target = self.targets.pop(0)
            self.attacks_made += 1
            await self.send({"type": "attack", "target_player_id": target})
        else:
            self.passes += 1
            await self.send({"type": "pass"})


# ---------------------------------------------------------------- games ----
@dataclass
class Game:
    run: Run
    index: int
    bots: list[Bot] = field(default_factory=list)
    code: str = ""
    session_id: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    failure: str | None = None

    @property
    def end(self) -> dict[str, Any] | None:
        return next((b.end for b in self.bots if b.end), None)

    async def play(self) -> None:
        run, http = self.run, self.run.http
        try:
            for b in self.bots:
                r = await http.post("/dev/guest", json={"display_name": b.name})
                r.raise_for_status()
                b.user_id = r.json()["user_id"]
            host = self.bots[0]
            r = await http.post(
                "/sessions",
                json={"locale": "en", "category_ids": run.category_ids, "config_overrides": run.overrides},
                headers={"X-User-Id": host.user_id},
            )
            r.raise_for_status()
            self.code, self.session_id = r.json()["join_code"], r.json()["session_id"]
            for b in self.bots:
                r = await http.post(
                    f"/sessions/{self.code}/join", json={"display_name": b.name}, headers={"X-User-Id": b.user_id}
                )
                r.raise_for_status()
                b.player_id, b.token = r.json()["player_id"], r.json()["player_token"]
            await asyncio.gather(*(b.connect() for b in self.bots))
            async with asyncio.timeout(30):
                await asyncio.gather(*(b.ready.wait() for b in self.bots))
            await asyncio.sleep(SYNC_BURST * SYNC_GAP_S + 0.2)  # first sync burst through the slowest wire
            self.started_at = time.monotonic()
            await host.send({"type": "start"})
            async with asyncio.timeout(run.game_timeout):
                await asyncio.gather(*(b.done.wait() for b in self.bots))
            self.ended_at = time.monotonic()
            if self.end is None:
                self.failure = "no end message"
        except Exception as e:  # noqa: BLE001 - one game failing must not hide the others
            self.failure = f"{type(e).__name__}: {e}"
            self.ended_at = time.monotonic()
        finally:
            await asyncio.gather(*(b.close() for b in self.bots), return_exceptions=True)


@dataclass
class Run:
    base: str
    http: httpx.AsyncClient
    category_ids: list[str]
    overrides: dict[str, Any]
    answer_key: dict[str, str]
    game_timeout: float
    games: list[Game] = field(default_factory=list)

    @property
    def ws_base(self) -> str:
        return "ws" + self.base[len("http"):]


async def load_answer_key(database_url: str, locale: str) -> dict[str, str]:
    """Question id -> the text of its correct option, so a bot with
    accuracy p can be right p of the time (the protocol never says which
    option is correct before the reveal)."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Question, QuestionTranslation

    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine)() as s:
            rows = await s.execute(
                select(Question.id, Question.correct_index, QuestionTranslation.options)
                .join(QuestionTranslation, QuestionTranslation.question_id == Question.id)
                .where(QuestionTranslation.locale == locale)
            )
            return {str(qid): options[correct] for qid, correct, options in rows}
    finally:
        await engine.dispose()


# ---------------------------------------------------------------- cpu ----
class CpuSampler:
    """`ps` on the API process once a second: cumulative CPU time and RSS."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.samples: list[tuple[float, float, int]] = []  # (monotonic, cpu seconds, rss kB)

    @staticmethod
    def _seconds(cputime: str) -> float:
        parts = [float(x) for x in cputime.split(":")]
        return sum(p * 60**i for i, p in enumerate(reversed(parts)))

    async def sample(self) -> None:
        p = await asyncio.create_subprocess_exec(
            "ps", "-p", str(self.pid), "-o", "cputime=", "-o", "rss=",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await p.communicate()
        fields = out.decode().split()
        if len(fields) == 2:
            self.samples.append((time.monotonic(), self._seconds(fields[0]), int(fields[1])))

    async def run(self) -> None:
        while True:
            await self.sample()
            await asyncio.sleep(1)

    def summary(self) -> dict[str, Any] | None:
        if len(self.samples) < 2:
            return None
        (t0, c0, _), (t1, c1, _) = self.samples[0], self.samples[-1]
        peaks = [
            (c - cp) / (t - tp) * 100
            for (tp, cp, _), (t, c, _) in zip(self.samples, self.samples[1:])
            if t > tp
        ]
        return {
            "mean_percent": (c1 - c0) / (t1 - t0) * 100 if t1 > t0 else 0.0,
            "peak_percent": max(peaks, default=0.0),
            "cpu_seconds": c1 - c0,
            "max_rss_mb": max(s[2] for s in self.samples) / 1024,
            "seconds": t1 - t0,
        }


# ---------------------------------------------------------------- report ----
def _mean(xs: list) -> float | None:
    return statistics.fmean(xs) if xs else None


def _pct(n: int, d: int) -> str:
    return f"{n / d * 100:.2f}%" if d else "n/a"


def build_report(run: Run, args: argparse.Namespace, cpu: dict[str, Any] | None) -> dict[str, Any]:
    bots = [b for g in run.games for b in g.bots]
    by_profile: dict[int, dict[str, Any]] = {}
    for profile in sorted({b.profile for b in bots}):
        answers = [a for b in bots if b.profile == profile for a in b.answers]
        on_time = [a for a in answers if a.on_time]
        late = [a for a in answers if not a.on_time]
        arrivals = [t.arrival_lag_ms for b in bots if b.profile == profile for t in b.transitions]
        rtts = [r for b in bots if b.profile == profile for r in b.rtts]
        by_profile[profile] = {
            "bots": sum(b.profile == profile for b in bots),
            "answers": len(answers),
            "on_time": len(on_time),
            "on_time_rejected": sum(a.accepted is False for a in on_time),
            "late": len(late),
            "late_rejected": sum(a.accepted is False for a in late),
            "late_accepted": sum(a.accepted is True for a in late),
            "overtaken": sum(b.overtaken for b in bots if b.profile == profile),
            "rejected_how": dict(Counter(a.how for a in answers if a.accepted is False)),
            "by_kind": {
                kind: {
                    "on_time": sum(a.kind == kind for a in on_time),
                    "on_time_rejected": sum(a.kind == kind and a.accepted is False for a in on_time),
                }
                for kind in ("question", "block")
            },
            # How much countdown the rejected on-time taps had left: the grace
            # that would have covered them is the profile's lag minus this.
            "rejected_on_time_countdowns_ms": sorted(a.countdown_ms for a in on_time if a.accepted is False),
            "closest_accepted_ms": min((a.countdown_ms for a in on_time if a.accepted), default=None),
            "arrival_lag_mean_ms": _mean(arrivals),
            "arrival_lag_max_ms": max(arrivals, default=None),
            "arrival_lag_count": len(arrivals),
            "sync_rtt_median_ms": statistics.median(rtts) if rtts else None,
        }
    ticks = [t.tick_lag_ms for g in run.games if g.bots for t in g.bots[0].transitions]
    ended = [g for g in run.games if g.end]
    results = [r for g in ended for r in g.end["results"]]  # type: ignore[index]
    tiebreaks = Counter(str(g.end["tiebreak"]) for g in ended)  # type: ignore[index]
    reasons = Counter(g.end["reason"] for g in ended)  # type: ignore[index]
    outcomes = Counter()
    for b in bots:
        outcomes.update(b.outcomes)
    summary = {
        "games": len(run.games),
        "finished": len(ended),
        "failed": [(g.code or f"#{g.index}", g.failure) for g in run.games if g.failure],
        "reasons": dict(reasons),
        "duration_s_mean": _mean([g.ended_at - g.started_at for g in ended if g.started_at]),
        "rounds": _mean([sum(g.bots[0].outcomes.values()) for g in ended]),
        "outcomes": dict(outcomes),
        "attacks": sum(b.attacks_made for b in bots),
        "passes": sum(b.passes for b in bots),
        "blocks": {"blocked": sum(b.blocked for b in bots), "not_blocked": sum(b.not_blocked for b in bots)},
        "winners_per_game": _mean([len(g.end["winner_ids"]) for g in ended]),  # type: ignore[index]
        "tiebreaks": dict(tiebreaks),
        "delta_mean": _mean([r["delta"] for r in results]),
        "delta_min": min((r["delta"] for r in results), default=None),
        "delta_max": max((r["delta"] for r in results), default=None),
        "hit_zero": sum(r["final_xp"] == 0 for r in results),
        "errors": dict(sum((b.errors for b in bots), Counter())),
        "per_game": [
            {
                "code": g.code,
                "duration_s": g.ended_at - g.started_at,
                "rounds": sum(g.bots[0].outcomes.values()),
                "winners": [r["display_name"] for r in g.end["results"] if r["player_id"] in g.end["winner_ids"]],
                "top_delta": g.end["results"][0]["delta"],
                "tiebreak": g.end["tiebreak"],
            }
            for g in ended
        ],
    }
    config = next((b.config for b in bots if b.config), {})
    return {
        "args": {k: v for k, v in vars(args).items() if k != "func"},
        "base": run.base,
        "config": config,
        "profiles": by_profile,
        "tick_lag": {"mean_ms": _mean(ticks), "max_ms": max(ticks, default=None), "count": len(ticks)},
        "cpu": cpu,
        "summary": summary,
        "bots": [
            {
                "game": g.code,
                "name": b.name,
                "profile": b.profile,
                "accuracy": b.accuracy,
                "observed_accuracy": (b.outcomes["correct"] / n) if (n := sum(b.outcomes.values())) else None,
                "answers": len(b.answers),
                "rejected": sum(a.accepted is False for a in b.answers),
            }
            for g in run.games
            for b in g.bots
        ],
    }


def render(rep: dict[str, Any]) -> str:
    L: list[str] = []
    cfg, s = rep["config"], rep["summary"]
    a = rep["args"]
    L.append(f"Bots run: {a['bots']} bots × {a['sessions']} sessions against {rep['base']}")
    L.append(
        f"  delay {a['delay']}, accuracy {a['accuracy']}, profiles {a['profile']}, attack {a['attack']}, "
        f"questions {a['questions']}"
    )
    if cfg:
        L.append(
            "  timers: pick {pick_seconds}s question {question_seconds}s reveal {reveal_seconds}s "
            "attack {attack_window_seconds}s block {block_seconds}s, "
            "grace base/min/max {grace_base_ms}/{grace_min_ms}/{grace_max_ms} ms".format(**cfg)
        )
    L.append("")
    L.append("== Answers rejected as late, per network profile ==")
    L.append(f"  {'profile':>8} {'bots':>4} {'answers':>7} {'on-time':>7} {'rejected':>8} {'rate':>7}   "
             f"{'late':>4} {'rejected':>8} {'accepted':>8} {'overtaken':>9}  closest ok  sync rtt")
    for profile, p in rep["profiles"].items():
        closest = p["closest_accepted_ms"]
        L.append(
            f"  {profile:>8} {p['bots']:>4} {p['answers']:>7} {p['on_time']:>7} {p['on_time_rejected']:>8} "
            f"{_pct(p['on_time_rejected'], p['on_time']):>7}   {p['late']:>4} {p['late_rejected']:>8} "
            f"{p['late_accepted']:>8} {p['overtaken']:>9}  "
            f"{(str(closest) + ' ms') if closest is not None else 'n/a':>10}  "
            f"{(str(int(p['sync_rtt_median_ms'])) + ' ms') if p['sync_rtt_median_ms'] is not None else 'n/a':>8}"
        )
        if p["rejected_how"]:
            L.append(f"           rejected as: {p['rejected_how']}")
        kinds = ", ".join(
            f"{k} {v['on_time_rejected']}/{v['on_time']}" for k, v in p["by_kind"].items() if v["on_time"]
        )
        left = p["rejected_on_time_countdowns_ms"]
        L.append(
            f"           on-time rejected by kind: {kinds}"
            + (f"; those taps had {min(left)}–{max(left)} ms of countdown left" if left else "")
        )
    L.append("  (on-time: the bot's countdown was above zero when it tapped; closest ok: the smallest")
    L.append("   countdown an accepted on-time answer was sent at; overtaken: the phase ended before the tap)")
    L.append("")
    L.append("== Phase-transition lag (timer-driven transitions only) ==")
    t = rep["tick_lag"]
    L.append(
        f"  tick lag (server stamps): mean {_fmt(t['mean_ms'])} ms, max {_fmt(t['max_ms'])} ms "
        f"over {t['count']} transitions"
    )
    for profile, p in rep["profiles"].items():
        L.append(
            f"  arrival lag at profile {profile:>3}: mean {_fmt(p['arrival_lag_mean_ms'])} ms, "
            f"max {_fmt(p['arrival_lag_max_ms'])} ms over {p['arrival_lag_count']} messages"
        )
    L.append("")
    L.append("== Server CPU ==")
    if rep["cpu"]:
        c = rep["cpu"]
        L.append(
            f"  mean {c['mean_percent']:.1f}% of one core over {c['seconds']:.0f} s "
            f"({c['cpu_seconds']:.1f} cpu-s), busiest second {c['peak_percent']:.0f}%, max RSS {c['max_rss_mb']:.0f} MB"
        )
    else:
        L.append("  not measured (no API process id: pass --server-pid with --base)")
    L.append("")
    L.append("== Game summary ==")
    L.append(f"  games {s['games']}, finished {s['finished']} {s['reasons']}, failed {len(s['failed'])}")
    for code, why in s["failed"]:
        L.append(f"    {code}: {why}")
    if s["finished"]:
        L.append(f"  mean duration {_fmt(s['duration_s_mean'], 1)} s, ~{_fmt(s['rounds'], 1)} rounds per game")
    L.append(f"  outcomes {s['outcomes']}")
    L.append(f"  attacks {s['attacks']}, passes {s['passes']}, blocks {s['blocks']}")
    L.append(
        f"  winners per game {_fmt(s['winners_per_game'], 2)}, tiebreaks {s['tiebreaks']}, "
        f"delta mean {_fmt(s['delta_mean'], 1)} (min {s['delta_min']}, max {s['delta_max']}), "
        f"hit zero {s['hit_zero']}"
    )
    if s["errors"]:
        L.append(f"  protocol errors seen by bots: {s['errors']}")
    for g in s["per_game"]:
        L.append(
            f"    {g['code']}: {g['rounds']} rounds in {g['duration_s']:.1f} s, "
            f"winner {', '.join(g['winners'])} ({g['top_delta']:+d}), tiebreak {g['tiebreak'] or 'none'}"
        )
    L.append("")
    L.append("== §8 targets ==")
    L.extend(targets(rep))
    return "\n".join(L)


def _fmt(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def targets(rep: dict[str, Any]) -> list[str]:
    out: list[str] = []
    p800 = rep["profiles"].get(800)
    cfg = rep["config"]
    grace = (cfg.get("grace_base_ms"), cfg.get("grace_min_ms"), cfg.get("grace_max_ms"))
    default_grace = grace == DEFAULT_GRACE
    if p800 and p800["on_time"]:
        rate = p800["on_time_rejected"] / p800["on_time"]
        verdict = "PASS" if rate < 0.02 else "FAIL"
        note = "" if default_grace else " (grace base/min/max {}/{}/{}, not the default {}/{}/{})".format(
            *grace, *DEFAULT_GRACE
        )
        out.append(
            f"  [{verdict}] 800±300: {p800['on_time_rejected']} of {p800['on_time']} on-time answers rejected "
            f"({rate * 100:.2f}%, target < 2%){note}"
        )
    else:
        out.append("  [skip] 800±300 rejection target: no bots on profile 800 answered")
    a = rep["args"]
    load = a["bots"] >= 20 and a["sessions"] >= 10
    p0 = (rep["profiles"].get(0) or {}).get("arrival_lag_mean_ms")
    lags = [x for x in (rep["tick_lag"]["mean_ms"], p0) if x is not None]
    if lags:
        worst = max(lags)
        verdict = "PASS" if worst < 100 else "FAIL"
        scale = "" if load else f" (measured at {a['bots']}×{a['sessions']}, the target is 20×10)"
        out.append(
            f"  [{verdict}] transition lag: tick mean {_fmt(rep['tick_lag']['mean_ms'])} ms, "
            f"profile-0 arrival mean {_fmt(p0)} ms (target < 100 ms){scale}"
        )
    else:
        out.append("  [skip] transition lag: no timer-driven transition observed")
    return out


# ---------------------------------------------------------------- main ----
async def run_all(args: argparse.Namespace, base: str, database_url: str, pid: int | None) -> dict[str, Any]:
    overrides = dict(json.loads(args.overrides)) if args.overrides else {}
    overrides.setdefault("question_count", args.questions)
    overrides.setdefault("min_players", min(2, args.bots))
    async with httpx.AsyncClient(base_url=base, timeout=30) as http:
        r = await http.get("/dev/categories")
        r.raise_for_status()
        category_ids = [c["id"] for c in r.json()]
        if not category_ids:
            raise SystemExit("the API has no active categories")
        key = await load_answer_key(database_url, "en")
        # Enough for every timer to run out on every round, then some.
        per_round = sum(
            overrides.get(f, d)
            for f, d in (("pick_seconds", 8), ("question_seconds", 10), ("reveal_seconds", 4),
                         ("attack_window_seconds", 6), ("block_seconds", 5))
        ) + 3
        run = Run(base, http, category_ids, overrides, key, game_timeout=per_round * args.questions + 60)
        delays = [parse_delay(d) for d in args.delay.split(",")]
        accuracies = [float(x) for x in args.accuracy.split(",")]
        profiles = [int(x) for x in args.profile.split(",")]
        for p in profiles:
            if p not in PROFILES:
                raise SystemExit(f"unknown profile {p}; choose from {sorted(PROFILES)}")
        seed = random.Random(args.seed)
        for gi in range(args.sessions):
            game = Game(run, gi)
            for bi in range(args.bots):
                game.bots.append(
                    Bot(
                        game,
                        name=f"bot{gi + 1}-{bi + 1}",
                        accuracy=cycle(accuracies, bi),
                        profile=cycle(profiles, bi),
                        delay=cycle(delays, bi),
                        attack=args.attack,
                        rng=random.Random(seed.random()),
                    )
                )
            run.games.append(game)
        sampler = CpuSampler(pid) if pid else None
        sampling = asyncio.create_task(sampler.run()) if sampler else None
        try:
            await asyncio.gather(*(g.play() for g in run.games))
        finally:
            if sampling:
                await asyncio.sleep(1.1)  # one more sample after the last game
                sampling.cancel()
        return build_report(run, args, sampler.summary() if sampler else None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bots", type=int, default=4, help="players per game (max_players is 20 by default)")
    ap.add_argument("--sessions", type=int, default=1, help="games to run at once")
    ap.add_argument("--accuracy", default="0.7", help="P(correct) per bot, a list cycles over the bots")
    ap.add_argument("--delay", default=HUMAN_DELAY, help='answer delay ms: "3500±1500", "500-9500" or "4000"')
    ap.add_argument("--profile", default="0", help="network profile(s): 0, 150, 400, 800; a list cycles")
    ap.add_argument("--attack", choices=["never", "random", "greedy"], default="greedy")
    ap.add_argument("--questions", type=int, default=10, help="question_count for the games")
    ap.add_argument("--overrides", help="JSON config_overrides merged into every game")
    ap.add_argument("--seed", type=int, default=1, help="seed for the bots' own randomness")
    ap.add_argument("--base", help="an API already running on the test DB (default: start one)")
    ap.add_argument("--server-pid", type=int, help="with --base: the API process to measure CPU on")
    ap.add_argument("--database-url", help="with --base: the test DB it uses, for the answer key")
    ap.add_argument("--port", type=int, default=8001, help="port for the API this script starts")
    ap.add_argument("--json", type=Path, help="also write the full report here")
    args = ap.parse_args()

    proc = None
    if args.base:
        base = args.base.rstrip("/")
        if urlsplit(base).netloc in DEV_PORTS:
            print("refusing to run bots against the dev API on :8000; start one on the test DB", file=sys.stderr)
            return 2
        from synthetic_serves import test_database_url

        database_url = args.database_url or test_database_url()
        pid = args.server_pid
    else:
        log = Path(tempfile.gettempdir()) / "bots.uvicorn.log"
        questions = max(120, args.questions * 6 + 40)  # pools for every category and a block reserve
        proc, database_url = start_api(args.port, log, questions=questions, host="127.0.0.1")
        base = f"http://127.0.0.1:{args.port}"
        pid = proc.pid
        if not wait_http(base + "/health", timeout=30):
            proc.terminate()
            print(f"the API did not come up on {base}; see {log}", file=sys.stderr)
            return 1
        print(f"started API {proc.pid} on {base} (test DB), log {log}", file=sys.stderr)
    try:
        rep = asyncio.run(run_all(args, base, database_url, pid))
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=10)
    print(render(rep))
    if args.json:
        args.json.write_text(json.dumps(rep, indent=2, default=str))
        print(f"\nfull report written to {args.json}")
    return 0 if not rep["summary"]["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
