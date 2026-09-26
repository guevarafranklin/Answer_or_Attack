"""Engine state and events as JSON (§3 snapshot, §6 event log).

`dump`/`load` turn a running game — the full `GameState`, the rng's
internal state, the runtime's applied-event counter and which questions
each player has been shown — into the string kept at
`session:{id}:state` and back. Nothing else of the runtime is in it:
sockets, timers and RTT trackers belong to a process, and a resumed
game measures afresh.

`encode_event`/`decode_event` are the `kind`/`payload` columns of
`session_events`: the event class name in lower case and its fields.

Both directions are explicit, field by field, so a change to the engine's
dataclasses fails the round-trip test rather than silently dropping a
field on the floor.
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from typing import Any

from app.game import engine as eng
from app.game.config import GameConfig
from app.game.engine import GameState, Phase

VERSION = 1


@dataclass(slots=True)
class Snapshot:
    seq: int  # the runtime's applied-event counter when the state was taken
    saved_ms: int
    state: GameState
    rng: random.Random
    shown: dict[str, set[str]]  # question ids each player has been shown (reports)


# ---------- snapshot ----------


def dump(
    state: GameState, rng: random.Random, seq: int, shown: dict[str, set[str]], saved_ms: int
) -> str:
    return json.dumps(
        {
            "v": VERSION,
            "seq": seq,
            "saved_ms": saved_ms,
            "state": dump_state(state),
            "rng": dump_rng(rng),
            "shown": {pid: sorted(qids) for pid, qids in shown.items()},
        },
        separators=(",", ":"),
    )


def load(raw: str | bytes) -> Snapshot:
    d = json.loads(raw)
    if d.get("v") != VERSION:
        raise ValueError(f"snapshot version {d.get('v')!r}, expected {VERSION}")
    return Snapshot(
        seq=d["seq"],
        saved_ms=d["saved_ms"],
        state=load_state(d["state"]),
        rng=load_rng(d["rng"]),
        shown={pid: set(qids) for pid, qids in d["shown"].items()},
    )


def dump_rng(rng: random.Random) -> list[Any]:
    version, internal, gauss_next = rng.getstate()
    return [version, list(internal), gauss_next]


def load_rng(data: list[Any]) -> random.Random:
    version, internal, gauss_next = data
    rng = random.Random()
    rng.setstate((version, tuple(internal), gauss_next))
    return rng


# ---------- state ----------


def dump_state(s: GameState) -> dict[str, Any]:
    return {
        "config": s.config.summary(),
        "pools": {cid: [_question(q) for q in qs] for cid, qs in s.pools.items()},
        "block_reserve": [_question(q) for q in s.block_reserve],
        "phase": s.phase.value,
        "round": s.round,
        "players": {pid: asdict(p) for pid, p in s.players.items()},  # join order
        "host_id": s.host_id,
        "picker_cursor": s.picker_cursor,
        "picker_id": s.picker_id,
        "board": list(s.board),
        "question": _question(s.question),
        "question_sent_ms": s.question_sent_ms,
        "answers": {pid: asdict(a) for pid, a in s.answers.items()},
        "attacks": [asdict(a) for a in s.attacks],
        "acted_this_window": sorted(s.acted_this_window),
        "blocks": {
            pid: {
                "target_id": b.target_id,
                "attacker_ids": list(b.attacker_ids),
                "question": _question(b.question),
                "answer": asdict(b.answer) if b.answer is not None else None,
            }
            for pid, b in s.blocks.items()
        },
        "deadline_ms": s.deadline_ms,
        "phase_end_ms": s.phase_end_ms,
        "low_presence_since_ms": s.low_presence_since_ms,
        "serves": [asdict(r) for r in s.serves],
        "results": [asdict(r) for r in s.results] if s.results is not None else None,
        "end_reason": s.end_reason,
    }


def load_state(d: dict[str, Any]) -> GameState:
    return GameState(
        config=GameConfig.from_overrides(d["config"]),
        pools={cid: [_load_question(q) for q in qs] for cid, qs in d["pools"].items()},
        block_reserve=[_load_question(q) for q in d["block_reserve"]],
        phase=Phase(d["phase"]),
        round=d["round"],
        players={pid: eng.Player(**p) for pid, p in d["players"].items()},
        host_id=d["host_id"],
        picker_cursor=d["picker_cursor"],
        picker_id=d["picker_id"],
        board=list(d["board"]),
        question=_load_question(d["question"]),
        question_sent_ms=d["question_sent_ms"],
        answers={pid: eng.AnswerRecord(**a) for pid, a in d["answers"].items()},
        attacks=[eng.PendingAttack(**a) for a in d["attacks"]],
        acted_this_window=set(d["acted_this_window"]),
        blocks={
            pid: eng.BlockChallenge(
                target_id=b["target_id"],
                attacker_ids=list(b["attacker_ids"]),
                question=_load_question(b["question"]),
                answer=eng.AnswerRecord(**b["answer"]) if b["answer"] is not None else None,
            )
            for pid, b in d["blocks"].items()
        },
        deadline_ms=d["deadline_ms"],
        phase_end_ms=d["phase_end_ms"],
        low_presence_since_ms=d["low_presence_since_ms"],
        serves=[eng.ServeRecord(**r) for r in d["serves"]],
        results=[eng.PlayerResult(**r) for r in d["results"]] if d["results"] is not None else None,
        end_reason=d["end_reason"],
    )


def _question(q: eng.Question | None) -> dict[str, Any] | None:
    return asdict(q) if q is not None else None


def _load_question(d: dict[str, Any] | None) -> eng.Question | None:
    return eng.Question(**d) if d is not None else None


# ---------- events ----------

_EVENT_TYPES: dict[str, type] = {
    cls.__name__.lower(): cls
    for cls in (
        eng.Join,
        eng.Start,
        eng.Pick,
        eng.Answer,
        eng.Attack,
        eng.Pass,
        eng.Disconnect,
        eng.Reconnect,
        eng.Tick,
    )
}


def encode_event(event: eng.Event) -> tuple[str, dict[str, Any]]:
    """(kind, payload) for a session_events row."""
    return type(event).__name__.lower(), asdict(event)


def decode_event(kind: str, payload: dict[str, Any]) -> eng.Event:
    try:
        cls = _EVENT_TYPES[kind]
    except KeyError:
        raise ValueError(f"unknown event kind {kind!r}") from None
    return cls(**payload)
