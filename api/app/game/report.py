"""A playtest report for one session, built from its event log (§6, §9).

The log is replayed through the pure engine one event at a time — the
same walk as `replay`, keeping the messages — and the report is what the
messages and `state.serves` say happened, timed by the log's server-time
stamps:

* per question (round questions and block questions): the stem and its
  length, the timer it ran on (`question_seconds` / `block_seconds`, plus
  the `grace_min_ms`–`grace_max_ms` allowance), when it was shown and
  revealed, and every player's
  outcome and response time (received − shown − rtt credit, as the
  engine recorded it);
* per attack window: when it opened, and for each token holder how long
  they took to attack or pass — or that they let the window expire;
* overall: timeout rate and p50/p90 response time (nearest-rank, over
  answered round questions; blocks separately), and per-player tallies.

The header carries the timers from the session's `resolved_config`, so a
report says which values that game actually used, and the overrides the
host asked for. Pure given the session row, the draw and the events;
`load_report` does the reads.
"""
from __future__ import annotations

import html
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.game import engine as eng
from app.game import snapshot
from app.game.config import GameConfig
from app.game.engine import Outcome, Phase
from app.game.replay import LoggedEvent, ReplayError, load_events
from app.game.seed import engine_rng
from app.models import GameSession
from app.models.sessions import BLOCK_POOL
from app.services import sessions as svc

# The config fields the report shows as "the timers this game used".
TIMER_FIELDS = (
    "pick_seconds",
    "question_seconds",
    "reveal_seconds",
    "attack_window_seconds",
    "block_seconds",
    "grace_base_ms",
    "grace_min_ms",
    "grace_max_ms",
    "rejoin_seconds",
    "abandon_seconds",
)
RULE_FIELDS = ("question_count", "min_players", "max_players", "board_size", "streak_for_token", "max_tokens")

# "pending": the log ends with the window still open (a report mid-game).
WindowAction = Literal["attack", "pass", "expired", "pending"]


@dataclass(slots=True)
class Serve:
    """One player's turn at one question."""

    player_id: str
    display_name: str
    outcome: Outcome
    response_ms: int | None  # None for timeout / absent
    points: int | None = None  # nominal, round questions only
    token_earned: bool = False


@dataclass(slots=True)
class QuestionReport:
    round: int
    kind: Literal["question", "block"]
    question_id: str
    category_id: str | None
    stem: str | None  # None when the draw has no text (should not happen)
    stem_length: int | None
    timer_seconds: int  # question_seconds or block_seconds
    grace_min_ms: int  # the per-player allowance runs from min to max (§5)
    grace_max_ms: int
    shown_ms: int
    deadline_ms: int
    phase_end_ms: int  # deadline + grace_max: when a Tick would close it
    resolved_ms: int | None = None  # reveal / block resolution
    target_id: str | None = None  # blocks: who had to answer
    attacker_ids: tuple[str, ...] = ()  # blocks: who attacked the target
    blocked: bool | None = None
    serves: list[Serve] = field(default_factory=list)

    @property
    def open_ms(self) -> int | None:
        """How long the question actually stayed open (early when everyone
        answered)."""
        return None if self.resolved_ms is None else self.resolved_ms - self.shown_ms


@dataclass(slots=True)
class HolderAction:
    player_id: str
    display_name: str
    tokens_at_open: int | None  # None: was not a holder when it opened (rejoined)
    action: WindowAction = "expired"
    target_id: str | None = None
    delay_ms: int | None = None  # from the window opening to the attack/pass
    absent_at_close: bool = False


@dataclass(slots=True)
class AttackWindow:
    round: int
    opened_ms: int
    deadline_ms: int
    phase_end_ms: int
    timer_seconds: int
    grace_min_ms: int
    grace_max_ms: int
    closed_ms: int | None = None
    holders: list[HolderAction] = field(default_factory=list)

    @property
    def expired_count(self) -> int:
        return sum(1 for h in self.holders if h.action == "expired")

    @property
    def open_ms(self) -> int | None:
        return None if self.closed_ms is None else self.closed_ms - self.opened_ms


@dataclass(slots=True)
class Distribution:
    """Outcomes and response times over a set of serves."""

    serves: int = 0  # served to a present player (absent excluded)
    correct: int = 0
    incorrect: int = 0
    timeout: int = 0
    absent: int = 0
    answered_ms: list[int] = field(default_factory=list)

    @property
    def timeout_rate(self) -> float | None:
        return None if self.serves == 0 else self.timeout / self.serves

    @property
    def p50_ms(self) -> int | None:
        return percentile(self.answered_ms, 0.5)

    @property
    def p90_ms(self) -> int | None:
        return percentile(self.answered_ms, 0.9)

    def add(self, s: Serve) -> None:
        setattr(self, s.outcome, getattr(self, s.outcome) + 1)
        if s.outcome != "absent":
            self.serves += 1
        if s.response_ms is not None:
            self.answered_ms.append(s.response_ms)


@dataclass(slots=True)
class PlayerSummary:
    player_id: str
    display_name: str
    questions: Distribution = field(default_factory=Distribution)
    blocks: Distribution = field(default_factory=Distribution)
    attacks: int = 0
    passes: int = 0
    expired: int = 0


@dataclass(slots=True)
class GameReport:
    session_id: str
    join_code: str
    status: str
    locale: str
    created_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    config: GameConfig
    resolved_config: dict[str, Any]
    config_overrides: dict[str, Any]
    players: list[PlayerSummary]
    questions: list[QuestionReport]
    windows: list[AttackWindow]
    overall: Distribution  # round questions
    blocks: Distribution
    phase: Phase
    round: int
    end_reason: eng.EndReason | None
    results: list[eng.PlayerResult] | None
    event_count: int
    started_ms: int | None  # the Start event's stamp: "t+" times count from it

    @property
    def windows_expired(self) -> int:
        return sum(w.expired_count for w in self.windows)

    @property
    def holders_total(self) -> int:
        return sum(len(w.holders) for w in self.windows)


def percentile(values: list[int], p: float) -> int | None:
    """Nearest-rank percentile: the smallest value with at least p of the
    sample at or below it. None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p * len(ordered)))
    return ordered[rank - 1]


# ---------- building ----------


async def load_report(db: AsyncSession, session: GameSession) -> GameReport:
    """Reads the draw and the log; raises svc.NotStarted for a lobby and
    ReplayError for a log with a gap."""
    draw = await svc.load_draw(db, session)
    events = await load_events(db, session)
    return build_report(session, draw, events)


def build_report(session: GameSession, draw: svc.Draw, events: list[LoggedEvent]) -> GameReport:
    cfg = draw.config
    texts = {str(q.id): q for q in draw.cache.questions} if draw.cache else {}
    state = eng.new_game(cfg, draw.pools, draw.block_reserve)
    rng = engine_rng(draw.seed)

    questions: list[QuestionReport] = []
    # Records still waiting for their reveal / resolution: the round
    # question by id, block questions by target (one per target per round).
    open_question: QuestionReport | None = None
    open_blocks: dict[str, QuestionReport] = {}
    windows: list[AttackWindow] = []
    window: AttackWindow | None = None
    serves_seen = 0
    started_ms: int | None = None

    def name(pid: str) -> str:
        p = state.players.get(pid)
        return p.display_name if p else pid

    def shown(kind: Literal["question", "block"], qid: str, deadline: int, at_ms: int) -> QuestionReport:
        text = texts.get(qid)
        category = None
        if kind == "question" or (text is not None and text.pool != BLOCK_POOL):
            category = text.pool if text else None
        q = QuestionReport(
            round=state.round,
            kind=kind,
            question_id=qid,
            category_id=category,
            stem=text.stem if text else None,
            stem_length=len(text.stem) if text else None,
            timer_seconds=cfg.question_seconds if kind == "question" else cfg.block_seconds,
            grace_min_ms=cfg.grace_min_ms,
            grace_max_ms=cfg.grace_max_ms,
            shown_ms=at_ms,
            deadline_ms=deadline,
            phase_end_ms=deadline + cfg.grace_max_ms,
        )
        questions.append(q)
        return q

    def close_window(at_ms: int) -> None:
        nonlocal window
        if window is None:
            return
        window.closed_ms = at_ms
        for h in window.holders:
            if h.action == "expired":
                p = state.players.get(h.player_id)
                h.absent_at_close = p is None or not p.active
        window = None

    def holder(pid: str) -> HolderAction:
        assert window is not None
        for h in window.holders:
            if h.player_id == pid:
                return h
        # Not a holder when the window opened: rejoined during it.
        h = HolderAction(pid, name(pid), None)
        window.holders.append(h)
        return h

    for _, at_ms, kind, payload in events:
        event = snapshot.decode_event(kind, payload)
        state, messages = eng.step(state, event, at_ms, rng, copy_state=False)
        if started_ms is None and state.phase is not Phase.LOBBY:
            started_ms = at_ms

        # Serves the engine recorded in this step, attached to their question.
        # (The reveal / resolution message follows in the same step.)
        for rec in state.serves[serves_seen:]:
            q = open_question if rec.kind == "question" else open_blocks.get(rec.player_id)
            if q is not None and q.question_id == rec.question_id:
                q.serves.append(Serve(rec.player_id, name(rec.player_id), rec.outcome, rec.response_ms))
        serves_seen = len(state.serves)

        for m in messages:
            match m:
                case eng.PhaseChanged(phase=Phase.ATTACK, deadline_ms=int() as deadline, phase_end_ms=int() as end):
                    close_window(at_ms)
                    window = AttackWindow(
                        state.round, at_ms, deadline, end, cfg.attack_window_seconds, cfg.grace_min_ms, cfg.grace_max_ms
                    )
                    # Whoever the window is waiting for as it opens.
                    window.holders = [
                        HolderAction(p.id, p.display_name, p.tokens)
                        for p in state.players.values()
                        if state.awaiting_attack(p.id)
                    ]
                    windows.append(window)
                case eng.PhaseChanged():
                    close_window(at_ms)
                case eng.AttackDeclared(attacker_id=pid, target_id=target) if window is not None:
                    h = holder(pid)
                    h.action, h.target_id, h.delay_ms = "attack", target, at_ms - window.opened_ms
                case eng.Passed(player_id=pid) if window is not None:
                    h = holder(pid)
                    h.action, h.delay_ms = "pass", at_ms - window.opened_ms
                case eng.QuestionShown(question_id=qid, deadline_ms=deadline):
                    open_question = shown("question", qid, deadline, at_ms)
                case eng.Revealed(outcomes=outcomes) if open_question is not None:
                    open_question.resolved_ms = at_ms
                    for s in open_question.serves:
                        o = outcomes.get(s.player_id)
                        if o is not None:
                            s.points, s.token_earned = o.points, o.token_earned
                    open_question = None
                case eng.BlockQuestionShown(target_id=target, question_id=qid, attacker_ids=attackers, deadline_ms=deadline):
                    q = open_blocks[target] = shown("block", qid, deadline, at_ms)
                    q.target_id, q.attacker_ids = target, tuple(attackers)
                case eng.BlockResolved(target_id=target, blocked=blocked):
                    # A block with nothing left to ask was never shown: no record.
                    q = open_blocks.pop(target, None)
                    if q is not None:
                        q.resolved_ms, q.blocked = at_ms, blocked
                case _:
                    pass

    if window is not None:  # the log ends mid-window: nobody has expired yet
        for h in window.holders:
            if h.action == "expired":
                h.action = "pending"

    overall, blocks = Distribution(), Distribution()
    players = {pid: PlayerSummary(pid, p.display_name) for pid, p in state.players.items()}
    for q in questions:
        for s in q.serves:
            (overall if q.kind == "question" else blocks).add(s)
            ps = players.get(s.player_id)
            if ps is not None:
                (ps.questions if q.kind == "question" else ps.blocks).add(s)
    for w in windows:
        for h in w.holders:
            ps = players.get(h.player_id)
            if ps is None:
                continue
            if h.action == "attack":
                ps.attacks += 1
            elif h.action == "pass":
                ps.passes += 1
            elif h.action == "expired":
                ps.expired += 1

    return GameReport(
        session_id=str(session.id),
        join_code=session.join_code,
        status=str(session.status),
        locale=str(session.locale),
        created_at=session.created_at,
        started_at=session.started_at,
        ended_at=session.ended_at,
        config=cfg,
        resolved_config=dict(session.resolved_config or {}),
        config_overrides=dict(session.config_overrides or {}),
        players=list(players.values()),
        questions=questions,
        windows=windows,
        overall=overall,
        blocks=blocks,
        phase=state.phase,
        round=state.round,
        end_reason=state.end_reason,
        results=state.results,
        event_count=len(events),
        started_ms=started_ms,
    )


# ---------- rendering ----------


def to_dict(r: GameReport) -> dict[str, Any]:
    """JSON-ready: dataclasses expanded, properties included, datetimes ISO."""
    d = asdict(r)
    d["config"] = asdict(r.config)
    for key in ("created_at", "started_at", "ended_at"):
        d[key] = getattr(r, key).isoformat() if getattr(r, key) else None
    d["phase"] = r.phase.value
    for q, qd in zip(r.questions, d["questions"]):
        qd["open_ms"] = q.open_ms
    for w, wd in zip(r.windows, d["windows"]):
        wd["open_ms"], wd["expired_count"] = w.open_ms, w.expired_count
    for dist, key in ((r.overall, "overall"), (r.blocks, "blocks")):
        d[key].update(timeout_rate=dist.timeout_rate, p50_ms=dist.p50_ms, p90_ms=dist.p90_ms)
    for p, pd in zip(r.players, d["players"]):
        for dist, key in ((p.questions, "questions"), (p.blocks, "blocks")):
            pd[key].update(timeout_rate=dist.timeout_rate, p50_ms=dist.p50_ms, p90_ms=dist.p90_ms)
    d["windows_expired"], d["holders_total"] = r.windows_expired, r.holders_total
    return d


def _ms(v: int | None) -> str:
    return "—" if v is None else f"{v} ms"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _timer(seconds: int, grace_min_ms: int, grace_max_ms: int) -> str:
    grace = f"{grace_min_ms}" if grace_min_ms == grace_max_ms else f"{grace_min_ms}–{grace_max_ms}"
    return f"{seconds} s + {grace} ms grace"


def _at(r: GameReport, at_ms: int) -> str:
    """A moment as seconds since the game started (raw epoch ms in JSON)."""
    if r.started_ms is None:
        return f"{at_ms} ms"
    return f"t+{(at_ms - r.started_ms) / 1000:.1f} s"


def _dist_line(d: Distribution) -> str:
    return (
        f"{d.serves} served · correct {d.correct} · incorrect {d.incorrect} · timeout {d.timeout}"
        f" ({_pct(d.timeout_rate)}) · absent {d.absent} · answered {len(d.answered_ms)}:"
        f" p50 {_ms(d.p50_ms)}, p90 {_ms(d.p90_ms)}"
    )


def timers_line(r: GameReport) -> str:
    return " · ".join(f"{f} {getattr(r.config, f)}" for f in TIMER_FIELDS)


def _short(pid: str) -> str:
    return pid[:8]


def _question_title(q: QuestionReport, name: dict[str, str]) -> str:
    if q.kind == "question":
        return f"Round {q.round} · question · category {_short(q.category_id or '?')}"
    target = name.get(q.target_id or "", q.target_id or "?")
    return f"Round {q.round} · block for {target} (attacked by {', '.join(name.get(a, a) for a in q.attacker_ids)})"


def render_text(r: GameReport) -> str:
    name = {p.player_id: p.display_name for p in r.players}
    out: list[str] = []
    out.append(f"Game {r.join_code} · session {r.session_id} · {r.status} · locale {r.locale}")
    out.append(
        f"phase {r.phase.value}, round {r.round}/{r.config.question_count}"
        + (f", ended: {r.end_reason}" if r.end_reason else "")
        + f" · {r.event_count} events"
    )
    out.append("timers (resolved_config): " + timers_line(r))
    out.append("rules: " + " · ".join(f"{f} {getattr(r.config, f)}" for f in RULE_FIELDS))
    out.append("overrides: " + (json.dumps(r.config_overrides, sort_keys=True) if r.config_overrides else "none"))
    out.append("players: " + ", ".join(f"{p.display_name} ({_short(p.player_id)})" for p in r.players))
    out.append("")

    out.append("== Questions ==")
    for q in r.questions:
        out.append(
            f"{_question_title(q, name)} · id {_short(q.question_id)} · stem {q.stem_length} chars"
            f" · timer {_timer(q.timer_seconds, q.grace_min_ms, q.grace_max_ms)} · shown {_at(r, q.shown_ms)} · open {_ms(q.open_ms)}"
            + (f" · {'blocked' if q.blocked else 'not blocked'}" if q.blocked is not None else "")
        )
        if q.stem:
            out.append(f"    “{q.stem}”")
        for s in q.serves:
            extra = f"  {s.points:+d}" if s.points is not None else ""
            extra += "  token" if s.token_earned else ""
            out.append(f"    {s.display_name:<16} {s.outcome:<9} {_ms(s.response_ms):>9}{extra}")
    out.append("")

    out.append("== Attack windows ==")
    if not r.windows:
        out.append("none (no token holder at any reveal)")
    for w in r.windows:
        out.append(
            f"Round {w.round} · opened {_at(r, w.opened_ms)} · timer {_timer(w.timer_seconds, w.grace_min_ms, w.grace_max_ms)}"
            f" · closed after {_ms(w.open_ms)} · {w.expired_count} of {len(w.holders)} holders let it expire"
        )
        for h in w.holders:
            tokens = "rejoined during window" if h.tokens_at_open is None else f"{h.tokens_at_open} token{'s' if h.tokens_at_open != 1 else ''}"
            if h.action == "attack":
                what = f"attacked {name.get(h.target_id or '', h.target_id)} after {_ms(h.delay_ms)}"
            elif h.action == "pass":
                what = f"passed after {_ms(h.delay_ms)}"
            elif h.action == "pending":
                what = "window still open"
            else:
                what = "let the window expire" + (" (absent at close)" if h.absent_at_close else "")
            out.append(f"    {h.display_name:<16} ({tokens})  {what}")
    out.append("")

    out.append("== Overall ==")
    out.append("questions: " + _dist_line(r.overall))
    out.append("blocks:    " + _dist_line(r.blocks))
    out.append(f"attack windows: {len(r.windows)} · holders {r.holders_total} · expired {r.windows_expired}")
    for p in r.players:
        d = p.questions
        out.append(
            f"    {p.display_name:<16} correct {d.correct} · incorrect {d.incorrect} · timeout {d.timeout}"
            f" · absent {d.absent} · p50 {_ms(d.p50_ms)} · p90 {_ms(d.p90_ms)}"
            f" · blocks {p.blocks.correct}/{p.blocks.serves} · attacks {p.attacks} · passes {p.passes} · expired {p.expired}"
        )
    if r.results:
        out.append("")
        out.append("== Results ==")
        for res in r.results:
            out.append(
                f"    {res.display_name:<16} {res.starting_xp} -> {res.final_xp}"
                f" (delta {res.delta:+d}, nominal {res.nominal_delta:+d})"
            )
    return "\n".join(out) + "\n"


_STYLE = """
body { font: 14px/1.4 system-ui, sans-serif; margin: 1rem auto; max-width: 64rem; padding: 0 1rem; color: #111; background: #fff; }
h1 { font-size: 1.3rem; } h2 { font-size: 1.05rem; margin-top: 1.6rem; }
.tablewrap { overflow-x: auto; margin: .4rem 0 1rem; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .2rem .5rem; border-bottom: 1px solid #e5e7eb; vertical-align: top; }
th { background: #f3f4f6; font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.meta { color: #555; } .stem { color: #444; font-style: italic; }
.correct { color: #15803d; } .incorrect { color: #b91c1c; } .timeout { color: #b45309; } .absent { color: #6b7280; }
.expired { color: #b45309; font-weight: 600; }
code { font-size: .9em; background: #f3f4f6; padding: 0 .2em; }
"""


def render_html(r: GameReport) -> str:
    e = html.escape
    name = {p.player_id: p.display_name for p in r.players}
    out: list[str] = [
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>Game report {e(r.join_code)}</title><style>{_STYLE}</style></head><body>",
        f"<h1>Game {e(r.join_code)} <span class='meta'>· {e(r.status)} · locale {e(r.locale)}</span></h1>",
        f"<p class='meta'>session <code>{e(r.session_id)}</code> · phase {e(r.phase.value)}, round {r.round}/{r.config.question_count}"
        + (f", ended: {e(r.end_reason)}" if r.end_reason else "")
        + f" · {r.event_count} events"
        + (f" · started {e(r.started_at.isoformat(timespec='seconds'))}" if r.started_at else "")
        + "</p>",
        "<h2>Timers this game used (resolved_config)</h2><table><tr>"
        + "".join(f"<th>{e(f)}</th>" for f in TIMER_FIELDS)
        + "</tr><tr>"
        + "".join(f"<td class='num'>{getattr(r.config, f)}</td>" for f in TIMER_FIELDS)
        + "</tr></table>",
        "<p class='meta'>rules: " + " · ".join(f"{e(f)} {getattr(r.config, f)}" for f in RULE_FIELDS) + "</p>",
        "<p class='meta'>overrides: <code>"
        + e(json.dumps(r.config_overrides, sort_keys=True) if r.config_overrides else "none")
        + "</code></p>",
        "<p>players: " + ", ".join(f"{e(p.display_name)} <code>{e(_short(p.player_id))}</code>" for p in r.players) + "</p>",
    ]

    out.append("<h2>Overall</h2><table><tr><th></th><th class='num'>served</th><th class='num'>correct</th><th class='num'>incorrect</th><th class='num'>timeout</th><th class='num'>timeout rate</th><th class='num'>absent</th><th class='num'>answered</th><th class='num'>p50</th><th class='num'>p90</th></tr>")
    for label, d in (("round questions", r.overall), ("block questions", r.blocks)):
        out.append(
            f"<tr><td>{label}</td><td class='num'>{d.serves}</td><td class='num'>{d.correct}</td><td class='num'>{d.incorrect}</td>"
            f"<td class='num'>{d.timeout}</td><td class='num'>{_pct(d.timeout_rate)}</td><td class='num'>{d.absent}</td>"
            f"<td class='num'>{len(d.answered_ms)}</td><td class='num'>{_ms(d.p50_ms)}</td><td class='num'>{_ms(d.p90_ms)}</td></tr>"
        )
    out.append("</table>")
    out.append(f"<p>attack windows: {len(r.windows)} · token holders {r.holders_total} · let it expire {r.windows_expired}</p>")
    out.append("<table><tr><th>player</th><th class='num'>correct</th><th class='num'>incorrect</th><th class='num'>timeout</th><th class='num'>absent</th><th class='num'>p50</th><th class='num'>p90</th><th class='num'>blocks won</th><th class='num'>attacks</th><th class='num'>passes</th><th class='num'>expired</th></tr>")
    for p in r.players:
        d = p.questions
        out.append(
            f"<tr><td>{e(p.display_name)}</td><td class='num'>{d.correct}</td><td class='num'>{d.incorrect}</td><td class='num'>{d.timeout}</td>"
            f"<td class='num'>{d.absent}</td><td class='num'>{_ms(d.p50_ms)}</td><td class='num'>{_ms(d.p90_ms)}</td>"
            f"<td class='num'>{p.blocks.correct}/{p.blocks.serves}</td><td class='num'>{p.attacks}</td><td class='num'>{p.passes}</td><td class='num'>{p.expired}</td></tr>"
        )
    out.append("</table>")

    out.append("<h2>Questions</h2>")
    for q in r.questions:
        out.append(
            f"<h3>{e(_question_title(q, name))}</h3><p class='meta'>id <code>{e(q.question_id)}</code> · stem {q.stem_length} chars"
            f" · timer {e(_timer(q.timer_seconds, q.grace_min_ms, q.grace_max_ms))} · shown {_at(r, q.shown_ms)} · open {_ms(q.open_ms)}"
            + (f" · <b>{'blocked' if q.blocked else 'not blocked'}</b>" if q.blocked is not None else "")
            + "</p>"
        )
        if q.stem:
            out.append(f"<p class='stem'>{e(q.stem)}</p>")
        out.append("<table><tr><th>player</th><th>outcome</th><th class='num'>response</th><th class='num'>points</th></tr>")
        for s in q.serves:
            points = "" if s.points is None else f"{s.points:+d}" + (" · token" if s.token_earned else "")
            out.append(
                f"<tr><td>{e(s.display_name)}</td><td class='{s.outcome}'>{s.outcome}</td>"
                f"<td class='num'>{_ms(s.response_ms)}</td><td class='num'>{points}</td></tr>"
            )
        out.append("</table>")

    out.append("<h2>Attack windows</h2>")
    if not r.windows:
        out.append("<p class='meta'>none (no token holder at any reveal)</p>")
    for w in r.windows:
        out.append(
            f"<h3>Round {w.round}</h3><p class='meta'>opened {_at(r, w.opened_ms)} · timer {e(_timer(w.timer_seconds, w.grace_min_ms, w.grace_max_ms))}"
            f" · closed after {_ms(w.open_ms)} · <span class='{'expired' if w.expired_count else ''}'>{w.expired_count} of {len(w.holders)} holders let it expire</span></p>"
        )
        out.append("<table><tr><th>holder</th><th class='num'>tokens</th><th>action</th><th class='num'>after</th></tr>")
        for h in w.holders:
            tokens = "rejoined" if h.tokens_at_open is None else str(h.tokens_at_open)
            if h.action == "attack":
                what = f"attacked {e(name.get(h.target_id or '', h.target_id or ''))}"
            elif h.action == "pass":
                what = "passed"
            elif h.action == "pending":
                what = "window still open"
            else:
                what = "<span class='expired'>expired</span>" + (" (absent at close)" if h.absent_at_close else "")
            out.append(f"<tr><td>{e(h.display_name)}</td><td class='num'>{tokens}</td><td>{what}</td><td class='num'>{_ms(h.delay_ms)}</td></tr>")
        out.append("</table>")

    if r.results:
        out.append("<h2>Results</h2><table><tr><th>player</th><th class='num'>start</th><th class='num'>final</th><th class='num'>delta</th><th class='num'>nominal</th></tr>")
        for res in r.results:
            out.append(
                f"<tr><td>{e(res.display_name)}</td><td class='num'>{res.starting_xp}</td><td class='num'>{res.final_xp}</td>"
                f"<td class='num'>{res.delta:+d}</td><td class='num'>{res.nominal_delta:+d}</td></tr>"
            )
        out.append("</table>")
    out.append("</body></html>")
    # Wide tables scroll inside their own box on a phone.
    return "\n".join(out).replace("<table>", "<div class='tablewrap'><table>").replace("</table>", "</table></div>")


__all__ = [
    "AttackWindow",
    "Distribution",
    "GameReport",
    "HolderAction",
    "QuestionReport",
    "ReplayError",
    "Serve",
    "build_report",
    "load_report",
    "percentile",
    "render_html",
    "render_text",
    "to_dict",
]
