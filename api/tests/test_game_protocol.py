"""app.game.protocol — the wire models and the per-recipient serializers.

The leak tests are the point of this file (spec §1 rule 3): seeded full
games with bots that answer, attack, pass, drop and come back, and after
*every* engine step a walker over every message to every player (plus the
reconnect `state` snapshot for every player) asserting that nothing
secret is in it. The walker is key-based: the test knows the engine's
secrets from the state and compares.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import ValidationError

from app.game import engine as eng
from app.game import protocol as proto
from app.game.config import GameConfig
from app.game.engine import (
    Answer,
    Attack,
    Disconnect,
    GameState,
    Join,
    Pass,
    Phase,
    Pick,
    Question,
    Reconnect,
    Start,
    Tick,
    new_game,
    step,
)
from app.schemas.sessions import SessionQuestionsCache
from scripts.simulate_balance import Bot, draw, make_bots

T0 = 1_700_000_000_000

# Keys that may only ever appear in `end` (or `state` at END).
END_ONLY_KEYS = {"final_xp", "xp", "total", "results", "winner_ids", "tiebreak"}
# Any key mentioning the secret start: starting_xp, starting_xp_choices, ...
START_PREFIX = "starting_xp"
# The answer, under any name.
ANSWER_KEYS = {"correct_option", "correct_index", "correct"}
# Private per-player counters: only ever the recipient's own.
PRIVATE_KEYS = {"tokens", "streak"}
# Nobody but the passer may learn about a pass.
PASS_KEYS = {"passed", "passer", "passed_ids", "pass"}


def walk(value: Any, path: tuple[Any, ...] = ()):
    """Every (path, key, value) in a dumped message."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield path, k, v
            yield from walk(v, path + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk(v, path + (i,))


def make_texts(pools: dict[str, list[Question]], reserve: list[Question]) -> dict[str, proto.QuestionText]:
    return {
        q.id: proto.QuestionText(stem=f"stem of {q.id}", options=[f"{q.id}/{i}" for i in range(4)])
        for q in [*reserve, *(q for pool in pools.values() for q in pool)]
    }


# ---------- the leak checker ----------


@dataclass
class Truth:
    """What the test knows that the clients must not: which questions have
    been revealed, which were block questions, who passed this step — and
    `unfloored`, each player's delta recomputed by the test from the
    engine's facts with the XP floor ignored (points, attack cost, block
    damage, steal). Every public delta must equal it: a delta that stops
    falling would say "this player is at 0"."""

    revealed: set[str]
    block_ids: set[str]
    unfloored: dict[str, int]
    # A target's unfloored delta right after their block resolved this
    # step (several blocks resolve in one step; a target may also be an
    # attacker in another and gain after their own result was stated).
    at_block: dict[str, int]

    @classmethod
    def empty(cls) -> Truth:
        return cls(set(), set(), {}, {})

    def apply(self, s: GameState, msgs: list[eng.Message]) -> None:
        acc = self.unfloored
        self.at_block = {}
        for m in msgs:
            match m:
                case eng.Revealed():
                    for pid, o in m.outcomes.items():
                        acc[pid] = acc.get(pid, 0) + o.points
                case eng.AttackDeclared():
                    acc[m.attacker_id] = acc.get(m.attacker_id, 0) - s.config.attack_cost
                case eng.BlockResolved():
                    if not m.blocked:
                        acc[m.target_id] = (
                            acc.get(m.target_id, 0) - s.config.attack_damage * len(m.attacker_ids)
                        )
                        for pid in m.attacker_ids:
                            acc[pid] = acc.get(pid, 0) + s.config.attack_steal
                    self.at_block[m.target_id] = acc.get(m.target_id, 0)

    def public_delta(self, pid: str) -> int:
        return self.unfloored.get(pid, 0)


def check_message(
    s: GameState, recipient: str, msg: proto.ServerMessage, truth: Truth, passers: set[str]
) -> None:
    data = msg.model_dump()
    kind = data["type"]
    ended = s.phase is Phase.END
    me = s.players.get(recipient)

    if kind == "pass_ack":
        assert recipient in passers, f"pass_ack to {recipient}, who did not pass"

    for path, key, value in walk(data):
        where = f"{kind} -> {recipient} at {path + (key,)}"

        if key in ANSWER_KEYS:
            assert kind == "reveal", f"answer outside reveal: {where}"
            assert data["question_id"] in truth.revealed, f"answer before reveal: {where}"
            assert data["question_id"] not in truth.block_ids, f"block answer revealed: {where}"

        if key.startswith(START_PREFIX) or key in END_ONLY_KEYS:
            assert ended, f"end-only key before the end: {where}"
            assert kind == "end" or (kind == "state" and data["phase"] == "end"), where

        if key in PRIVATE_KEYS:
            assert path in ((), ("you",)), f"private counter in a shared structure: {where}"
            assert kind in ("reveal", "state"), where
            assert me is not None, where  # a stranger's `state` has no `you`
            assert value == getattr(me, key), f"{where}: {value} != own {getattr(me, key)}"

        assert key not in PASS_KEYS, f"who passed leaked: {where}"

        # Every delta shown before the end is the unfloored one.
        if key == "delta" and not ended and value is not None:
            if kind == "block_result":
                assert path == () and value == truth.at_block[data["target_id"]], where
                continue
            if path == ():  # reveal: own
                owner = recipient
            elif len(path) == 2 and path[0] == "players":  # lobby / state roster
                owner = data["players"][path[1]]["player_id"]
            else:
                raise AssertionError(f"unexpected delta: {where}")
            assert value == truth.public_delta(owner), f"floored delta shown: {where}"
        if key == "deltas":
            assert value == {pid: truth.public_delta(pid) for pid in value}, where

    if kind == "block_question":
        assert recipient in s.blocks, f"block question to a non-target {recipient}"
    if kind == "question":
        assert data["question_id"] not in truth.block_ids, "block question text broadcast"
    if kind == "block_result":
        if recipient == data["target_id"]:
            assert data["damage"] is not None and data["delta"] is not None
            assert data["gained"] is None
        elif recipient in data["attacker_ids"]:
            assert data["damage"] is None and data["delta"] is None
            assert data["gained"] is not None
        else:
            assert data["damage"] is None and data["delta"] is None and data["gained"] is None
    if kind == "state":
        check_state(s, recipient, data)


def check_state(s: GameState, recipient: str, data: dict[str, Any]) -> None:
    me = s.players.get(recipient)
    assert data["phase"] == s.phase
    assert [p["player_id"] for p in data["players"]] == list(s.players)
    for view in data["players"]:
        p = s.players[view["player_id"]]
        assert view["delta"] == (None if s.phase is Phase.LOBBY else p.nominal_delta)
        assert set(view) == {"player_id", "display_name", "present", "dropped", "is_host", "delta"}
    if me is None or me.dropped:
        assert data["you"] is None and data["question"] is None and data["block_question"] is None
        return
    if s.phase is Phase.QUESTION:
        assert data["question"]["question_id"] == s.question.id
        assert data["you"]["answered"] == (recipient in s.answers)
    else:
        assert data["question"] is None
    if s.phase is Phase.BLOCK and recipient in s.blocks and s.blocks[recipient].question is not None:
        assert data["block_question"]["question_id"] == s.blocks[recipient].question.id
        assert data["you"]["answered"] == (s.blocks[recipient].answer is not None)
    else:
        assert data["block_question"] is None
    if s.phase is Phase.ATTACK:
        assert data["you"]["acted"] == (recipient in s.acted_this_window)
        assert len(data["attacks"]) == len(s.attacks)
    if s.phase is Phase.END:
        assert data["end"]["results"][0]["player_id"] == s.results[0].player_id
    else:
        assert data["end"] is None


# ---------- the bot driver ----------


@dataclass
class Trace:
    steps: int = 0
    passes: int = 0
    attacks: int = 0
    blocks: int = 0
    reveals: int = 0
    absences: int = 0
    drops: int = 0
    reconnects: int = 0
    floored: int = 0  # steps in which someone's real delta sat above the nominal one
    hit_zero: set[str] = field(default_factory=set)
    ended: eng.Ended | None = None


def play(
    seed: int,
    players: int,
    *,
    config: GameConfig | None = None,
    observe=None,
    churn: float = 0.15,
) -> tuple[GameState, Trace]:
    """A full seeded game, bots modelled on the balance simulator plus
    disconnects: with probability `churn` per question somebody steps
    out and usually comes back a phase later; one player, if there are
    enough, never comes back and is dropped. `observe(state, engine
    messages, outbox)` runs after every step."""
    # A round is at most ~33 s: a churned player is back well within the
    # rejoin window, the one who leaves for good is dropped a round later.
    config = config or GameConfig(rejoin_seconds=20)
    bot_rng = random.Random(seed)
    engine_rng = random.Random(seed ^ 0x5EED)
    pools, reserve = draw(config, bot_rng)
    texts = make_texts(pools, reserve)
    state = new_game(config, pools, reserve)
    bots = make_bots(players, bot_rng)
    by_id = {b.id: b for b in bots}
    trace = Trace()
    truth = Truth.empty()
    now = T0
    away: list[str] = []  # absent bots due back next phase
    gone: str | None = bots[-1].id if players >= 5 else None  # leaves mid-game for good

    def send(event: eng.Event, at: int | None = None) -> list[eng.Message]:
        nonlocal now, state
        if at is not None:
            now = max(now, at)
        state, msgs = step(state, event, now, engine_rng, copy_state=False)
        passers = {m.player_id for m in msgs if isinstance(m, eng.Passed)}
        for m in msgs:
            match m:
                case eng.Revealed():
                    truth.revealed.add(m.question_id)
                    trace.reveals += 1
                case eng.BlockQuestionShown():
                    truth.block_ids.add(m.question_id)
                    trace.blocks += 1
                case eng.AttackDeclared():
                    trace.attacks += 1
                case eng.Passed():
                    trace.passes += 1
                case eng.PresenceChanged():
                    trace.absences += m.status == "absent"
                    trace.drops += m.status == "dropped"
                    trace.reconnects += m.status == "returned"
                case eng.Ended():
                    trace.ended = m
        truth.apply(state, msgs)
        if state.phase is not Phase.LOBBY:
            for p in state.players.values():
                assert p.nominal_delta == truth.public_delta(p.id)
                assert p.delta >= p.nominal_delta  # the floor only ever helps
                trace.hit_zero.update([p.id] if p.xp == 0 else [])
            trace.floored += any(p.delta != p.nominal_delta for p in state.players.values())
        outbox = proto.fan_out(state, msgs, texts)
        assert set(outbox) >= set(state.players)
        for pid, out in outbox.items():
            for msg in out:
                check_message(state, pid, msg, truth, passers)
        for pid in state.players:
            check_message(state, pid, proto.state_message(state, pid, texts), truth, passers)
        if observe is not None:
            observe(state, msgs, outbox)
        trace.steps += 1
        return msgs

    for b in bots:
        send(Join(b.id, f"Bot {b.id}"))
    send(Start(bots[0].id))

    def come_back() -> None:
        while away:
            send(Reconnect(away.pop()))

    while state.phase is not Phase.END:
        phase = state.phase
        if phase is Phase.PICK:
            come_back()
            send(Pick(state.picker_id, bot_rng.choice(state.board)))
        elif phase is Phase.QUESTION:
            q = state.question
            if bot_rng.random() < churn:
                candidates = [p.id for p in state.active_players() if p.id != gone]
                if candidates:
                    pid = bot_rng.choice(candidates)
                    send(Disconnect(pid))
                    away.append(pid)
            if gone is not None and state.round == 3 and state.players[gone].present:
                send(Disconnect(gone))
            plan = []
            for p in state.active_players():
                b = by_id[p.id]
                at = state.question_sent_ms + b.response_ms(bot_rng)
                if at <= state.phase_end_ms:
                    right = bot_rng.random() < b.p_correct(q.difficulty)
                    option = q.correct_option if right else (q.correct_option + 1) % 4
                    plan.append((at, p.id, option))
            for at, pid, option in sorted(plan):
                if state.phase is not Phase.QUESTION:
                    break
                send(Answer(pid, q.id, option), at=at)
            if state.phase is Phase.QUESTION:
                send(Tick(), at=state.phase_end_ms)
        elif phase is Phase.ATTACK:
            holders = [p.id for p in state.active_players() if state.awaiting_attack(p.id)]
            bot_rng.shuffle(holders)
            for pid in holders:
                if state.phase is not Phase.ATTACK:
                    break
                act_in_attack_window(state, by_id[pid], bot_rng, send)
            if state.phase is Phase.ATTACK:
                send(Tick(), at=state.phase_end_ms)
        elif phase is Phase.BLOCK:
            plan = []
            for target_id, block in state.blocks.items():
                b, q = by_id[target_id], block.question
                at = state.question_sent_ms + b.response_ms(bot_rng)
                if q is not None and at <= state.phase_end_ms and state.players[target_id].active:
                    right = bot_rng.random() < b.p_correct(q.difficulty)
                    option = q.correct_option if right else (q.correct_option + 1) % 4
                    plan.append((at, target_id, q.id, option))
            for at, pid, qid, option in sorted(plan):
                if state.phase is not Phase.BLOCK:
                    break
                send(Answer(pid, qid, option), at=at)
            if state.phase is Phase.BLOCK:
                send(Tick(), at=state.phase_end_ms)
        else:  # REVEAL
            come_back()
            send(Tick(), at=state.phase_end_ms)

    send(Tick())  # END is inert; one more snapshot check
    return state, trace


def act_in_attack_window(state: GameState, bot: Bot, rng: random.Random, send) -> None:
    if bot.policy == "never":
        send(Pass(bot.id))
        return
    others = [p for p in state.active_players() if p.id != bot.id]
    if bot.policy == "greedy":
        others.sort(key=lambda p: -p.nominal_delta)
    else:
        rng.shuffle(others)
    for target in others:
        msgs = send(Attack(bot.id, target.id))
        codes = [m.code for m in msgs if isinstance(m, eng.Error)]
        if not codes:
            return
        if codes != ["target_full"]:
            break
    if state.phase is Phase.ATTACK:
        send(Pass(bot.id))


# ---------- leak tests over full games ----------


@pytest.mark.parametrize("players", [2, 3, 5, 8, 12])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_no_secret_reaches_any_player_in_a_full_game(players, seed):
    state, trace = play(seed, players)
    assert state.phase is Phase.END and trace.ended is not None
    assert trace.reveals >= state.config.question_count
    assert trace.steps > 40


def test_the_games_exercise_every_secret_bearing_path():
    """The parametrised games are only a leak test if passes, attacks,
    blocks, absences, drops and reconnects all actually happened."""
    total = Trace()
    for seed in (1, 2, 3):
        for players in (5, 8, 12):
            _, t = play(seed, players)
            for f in ("passes", "attacks", "blocks", "absences", "drops", "reconnects"):
                setattr(total, f, getattr(total, f) + getattr(t, f))
    assert total.passes > 20
    assert total.attacks > 20
    assert total.blocks > 10
    assert total.absences > 5
    assert total.reconnects > 5
    assert total.drops >= 3


# Starts near the floor with heavy losses, so the floor bites in every
# parametrised game below (the driver asserts it did).
LOW_XP = GameConfig(
    rejoin_seconds=20,
    starting_xp_choices=(0, 1, 2),
    points_wrong=-4,
    points_timeout=-4,
    attack_damage=5,
)


@pytest.mark.parametrize("players", [4, 6, 10])
@pytest.mark.parametrize("seed", [11, 12, 13])
def test_public_deltas_ignore_the_floor(players, seed):
    """Games where players hit 0: the sequence of deltas the room saw for
    each player (every `reveal`) must be identical to one computed from
    the engine's facts with the floor ignored — and the final screen
    shows both the real and the nominal delta."""
    shown: dict[str, list[int]] = {}
    expected: dict[str, list[int]] = {}
    unfloored = Truth.empty()  # accumulates points/cost/damage/steal, never floors

    def observe(s: GameState, msgs, outbox):
        unfloored.apply(s, msgs)
        for m in msgs:
            if isinstance(m, eng.Revealed):
                reveal = next(x for x in outbox[pid0] if isinstance(x, proto.RevealOut))
                for pid, d in reveal.deltas.items():
                    shown.setdefault(pid, []).append(d)
                    expected.setdefault(pid, []).append(unfloored.public_delta(pid))

    pid0 = make_bots(1, random.Random(0))[0].id
    state, trace = play(seed, players, config=LOW_XP, observe=observe)
    absorbed = [p.id for p in state.players.values() if p.delta > p.nominal_delta]
    assert absorbed and trace.floored > 0, "the low-XP games are meant to reach the floor"
    assert set(absorbed) <= trace.hit_zero
    assert shown == expected
    for r in trace.ended.results:
        assert r.delta == r.final_xp - r.starting_xp
        assert r.nominal_delta == unfloored.public_delta(r.player_id)
        assert r.delta >= r.nominal_delta


def test_abandoned_game_ends_cleanly_for_everyone():
    """Everyone but one leaves; the abandon timer ends the game and the
    `end` message reaches all players, the absent ones included."""
    config = GameConfig(rejoin_seconds=600, abandon_seconds=5)
    pools, reserve = draw(config, random.Random(0))
    texts = make_texts(pools, reserve)
    s = new_game(config, pools, reserve)
    rng = random.Random(0)
    now = T0
    for pid in ("a", "b", "c"):
        s, _ = step(s, Join(pid, pid), now, rng, copy_state=False)
    s, _ = step(s, Start("a"), now, rng, copy_state=False)
    s, _ = step(s, Disconnect("b"), now, rng, copy_state=False)
    s, _ = step(s, Disconnect("c"), now, rng, copy_state=False)
    s, msgs = step(s, Tick(), now + 6_000, rng, copy_state=False)
    assert s.phase is Phase.END and s.end_reason == "abandoned"
    out = proto.fan_out(s, msgs, texts)
    for pid in ("a", "b", "c"):
        end = [m for m in out[pid] if isinstance(m, proto.EndOut)]
        assert len(end) == 1 and end[0].reason == "abandoned"
        check_message(s, pid, end[0], Truth.empty(), set())
        check_message(s, pid, proto.state_message(s, pid, texts), Truth.empty(), set())


def test_reveal_and_end_payloads_are_complete_and_correct():
    """The positive side: the reveal really carries the answer and every
    delta, and the end really carries the full standing."""
    seen: dict[str, Any] = {"reveals": 0}

    def observe(s: GameState, msgs, outbox):
        for m in msgs:
            if isinstance(m, eng.Revealed):
                for pid, out in outbox.items():
                    reveal = next(x for x in out if isinstance(x, proto.RevealOut))
                    assert reveal.correct_option == m.correct_option
                    assert reveal.deltas == {p: o.delta for p, o in m.outcomes.items()}
                    if pid in m.outcomes:
                        assert reveal.tokens == m.outcomes[pid].tokens
                        assert reveal.outcome == m.outcomes[pid].outcome
                    else:
                        assert reveal.outcome is None and reveal.points == 0
                seen["reveals"] += 1
            if isinstance(m, eng.Ended):
                for out in outbox.values():
                    end = next(x for x in out if isinstance(x, proto.EndOut))
                    assert [r.player_id for r in end.results] == [r.player_id for r in m.results]
                    assert {r.player_id: r.starting_xp for r in end.results} == {
                        p.id: p.starting_xp for p in s.players.values()
                    }
                    assert end.winner_ids == list(m.winner_ids)

    play(4, 6, observe=observe)
    assert seen["reveals"] >= 15


# ---------- per-recipient routing, unit level ----------


CFG = GameConfig(starting_xp_choices=(10,), min_players=2, rejoin_seconds=2)


def _pools() -> tuple[dict[str, list[Question]], list[Question]]:
    pools = {c: [Question(f"{c}-{i}", c, 1 + i // 3, i % 4) for i in range(6)] for c in ("g", "h", "s")}
    return pools, [Question(f"blk-{i}", "h", 2, i % 4) for i in range(4)]


class Game:
    def __init__(self, players=("a", "b", "c")):
        self.pools, self.reserve = _pools()
        self.texts = make_texts(self.pools, self.reserve)
        self.s = new_game(CFG, self.pools, self.reserve)
        self.rng = random.Random(1)
        self.now = T0
        for pid in players:
            self.send(Join(pid, pid.upper()))

    def send(self, event: eng.Event, at: int | None = None) -> proto.Outbox:
        if at is not None:
            self.now = max(self.now, at)
        self.s, self.msgs = step(self.s, event, self.now, self.rng, copy_state=False)
        self.out = proto.fan_out(self.s, self.msgs, self.texts)
        return self.out

    def tick(self) -> proto.Outbox:
        return self.send(Tick(), at=self.s.phase_end_ms)

    def start(self) -> proto.Outbox:
        return self.send(Start("a"))

    def to_question(self) -> proto.Outbox:
        self.start()
        return self.send(Pick(self.s.picker_id, self.s.board[0]))

    def to_attack(self, *right: str) -> proto.Outbox:
        """Through two rounds so `right` earn a token, then into the window."""
        for _ in range(2):
            if self.s.phase is Phase.LOBBY:
                self.to_question()
            else:
                self.send(Pick(self.s.picker_id, self.s.board[0]))
            q = self.s.question
            for pid in self.s.players:
                option = q.correct_option if pid in right else (q.correct_option + 1) % 4
                self.send(Answer(pid, q.id, option))  # the last answer closes the question
            assert self.s.phase is Phase.REVEAL, self.s.phase
            self.tick()  # REVEAL -> ATTACK or next PICK
        assert self.s.phase is Phase.ATTACK, self.s.phase
        return self.out


def kinds(msgs: list[proto.ServerMessage]) -> list[str]:
    return [m.type for m in msgs]


def test_joins_and_host_changes_send_a_fresh_lobby_to_everyone():
    g = Game(players=())
    out = g.send(Join("a", "A"))
    assert kinds(out["a"]) == ["lobby"]
    out = g.send(Join("b", "B"))
    assert kinds(out["a"]) == kinds(out["b"]) == ["lobby"]
    lobby = out["b"][0]
    assert lobby.host_id == "a"
    assert [(p.player_id, p.is_host, p.delta) for p in lobby.players] == [("a", True, None), ("b", False, None)]
    assert "starting_xp_choices" not in lobby.config and "starting_xp_tiers" not in lobby.config
    assert lobby.config["question_count"] == CFG.question_count


def test_errors_go_only_to_the_offender_even_when_not_in_the_game():
    g = Game()
    g.start()
    out = g.send(Join("late", "Late"))
    assert kinds(out["late"]) == ["error"] and out["late"][0].code == "wrong_phase"
    assert out["a"] == out["b"] == out["c"] == []
    out = g.send(Pick("b", g.s.board[0]))
    assert kinds(out["b"]) == ["error"] and out["b"][0].code == "not_your_pick"
    assert out["a"] == [] and out["c"] == []


def test_question_text_is_looked_up_and_the_answer_is_not_in_it():
    g = Game()
    out = g.to_question()
    q = g.s.question
    for pid in "abc":
        msg = next(m for m in out[pid] if isinstance(m, proto.QuestionOut))
        assert msg.question_id == q.id
        assert msg.stem == f"stem of {q.id}"
        assert msg.options == [f"{q.id}/{i}" for i in range(4)]
        assert "correct_option" not in msg.model_dump()


def test_answer_ack_is_private():
    g = Game()
    g.to_question()
    q = g.s.question
    out = g.send(Answer("b", q.id, 0))
    assert kinds(out["b"]) == ["answer_ack"] and out["b"][0].accepted
    assert out["a"] == [] and out["c"] == []


def test_reveal_carries_own_tokens_and_everyones_deltas():
    g = Game()
    g.to_question()
    q = g.s.question
    g.send(Answer("a", q.id, q.correct_option))
    g.send(Answer("b", q.id, (q.correct_option + 1) % 4))
    out = g.tick()
    for pid in "abc":
        reveal = next(m for m in out[pid] if isinstance(m, proto.RevealOut))
        assert reveal.correct_option == q.correct_option
        assert set(reveal.deltas) == {"a", "b", "c"}
        assert reveal.tokens == g.s.players[pid].tokens
        assert reveal.streak == g.s.players[pid].streak
    assert next(m for m in out["a"] if isinstance(m, proto.RevealOut)).outcome == "correct"
    assert next(m for m in out["c"] if isinstance(m, proto.RevealOut)).outcome == "timeout"


def test_pass_is_acknowledged_to_the_passer_only():
    g = Game()
    g.to_attack("a", "b")
    assert g.s.players["a"].tokens == 1 and g.s.players["b"].tokens == 1
    out = g.send(Pass("a"))
    assert kinds(out["a"]) == ["pass_ack"]
    assert out["b"] == [] and out["c"] == []


def test_attacks_are_public_and_block_questions_private():
    g = Game()
    g.to_attack("a", "b")
    out = g.send(Attack("a", "c"))
    for pid in "abc":
        assert kinds(out[pid]) == ["attacks"]
        assert out[pid][0].attacks == [proto.AttackView(attacker_id="a", target_id="c")]
    out = g.send(Pass("b"))  # last holder acted: the window closes, BLOCK opens
    assert g.s.phase is Phase.BLOCK
    assert kinds(out["b"]) == ["pass_ack", "phase"]
    assert kinds(out["a"]) == ["phase"]
    assert kinds(out["c"]) == ["phase", "block_question"]
    bq = out["c"][1]
    assert bq.attacker_ids == ["a"] and bq.stem == f"stem of {bq.question_id}"
    assert bq.question_id in {q.id for q in g.reserve}


def test_block_result_shows_damage_to_the_target_and_gain_to_the_attacker():
    g = Game()
    g.to_attack("a", "b")
    g.send(Attack("a", "c"))
    g.send(Attack("b", "c"))
    assert g.s.phase is Phase.BLOCK
    before = g.s.players["c"].delta
    out = g.tick()  # c never answers
    a, b, c = (next(m for m in out[p] if isinstance(m, proto.BlockResultOut)) for p in "abc")
    assert not c.blocked and c.damage == 2 * CFG.attack_damage and c.gained is None
    assert c.delta == before - c.damage == g.s.players["c"].delta
    assert a.gained == CFG.attack_steal and a.damage is None and a.delta is None
    assert b.gained == CFG.attack_steal
    assert a.attacker_ids == ["a", "b"] and a.target_id == "c"


def test_block_result_on_a_successful_block():
    g = Game()
    g.to_attack("a")
    g.send(Attack("a", "b"))
    bq = next(m for m in g.out["b"] if isinstance(m, proto.BlockQuestionOut))
    q = next(x for x in g.reserve if x.id == bq.question_id)
    out = g.send(Answer("b", q.id, q.correct_option))
    a, b, c = (next(m for m in out[p] if isinstance(m, proto.BlockResultOut)) for p in "abc")
    assert b.blocked and b.damage == 0 and b.delta == g.s.players["b"].delta
    assert a.gained == 0
    assert c.damage is None and c.gained is None


def test_presence_and_phase_are_broadcast():
    g = Game()
    g.start()
    out = g.send(Disconnect("b"))
    for pid in "abc":
        assert kinds(out[pid]) == ["presence"]
        assert out[pid][0].status == "absent"
    out = g.send(Tick(), at=g.now + 2_000)
    assert all(m.status == "dropped" for pid in "abc" for m in out[pid])


def test_end_is_the_full_reveal():
    g = Game(players=("a", "b"))
    g.start()
    g.send(Disconnect("b"))
    out = g.send(Tick(), at=g.now + CFG.abandon_seconds * 1000)
    assert g.s.phase is Phase.END
    end = next(m for m in out["a"] if isinstance(m, proto.EndOut))
    assert end.reason == "abandoned"
    assert {r.player_id: (r.starting_xp, r.final_xp, r.delta) for r in end.results} == {
        "a": (10, 10, 0),
        "b": (10, 10, 0),
    }
    assert set(end.winner_ids) == {"a", "b"} and end.tiebreak == "shared"


def test_unknown_engine_message_is_refused():
    g = Game()

    class Weird:
        pass

    with pytest.raises(TypeError):
        proto.fan_out(g.s, [Weird()], g.texts)  # type: ignore[list-item]


# ---------- the reconnect snapshot ----------


def test_state_in_the_lobby():
    g = Game()
    st = proto.state_message(g.s, "b", g.texts)
    assert st.phase is Phase.LOBBY and st.host_id == "a" and st.you == proto.YouView(
        tokens=0, streak=0, answered=False, acted=False
    )
    assert [p.delta for p in st.players] == [None, None, None]
    assert st.question is None and st.end is None and st.attacks == []
    assert "starting_xp_choices" not in st.config


def test_state_for_a_stranger_has_no_private_slice():
    g = Game()
    g.to_question()
    st = proto.state_message(g.s, "nobody", g.texts)
    assert st.you is None and st.question is None
    assert st.phase is Phase.QUESTION


def test_state_during_a_question_says_whether_you_answered():
    g = Game()
    g.to_question()
    q = g.s.question
    g.send(Answer("a", q.id, 1))
    a = proto.state_message(g.s, "a", g.texts)
    b = proto.state_message(g.s, "b", g.texts)
    assert a.you.answered and not b.you.answered
    assert a.question == b.question
    assert a.question.question_id == q.id and a.question.deadline_ms == g.s.deadline_ms
    assert "correct_option" not in a.model_dump_json()


def test_state_during_a_block_only_tells_the_target_the_question():
    g = Game()
    g.to_attack("a", "b")
    g.send(Attack("a", "c"))
    g.send(Pass("b"))
    assert g.s.phase is Phase.BLOCK
    c = proto.state_message(g.s, "c", g.texts)
    a = proto.state_message(g.s, "a", g.texts)
    assert c.block_question is not None and c.block_question.attacker_ids == ["a"]
    assert a.block_question is None
    assert a.attacks == c.attacks == [proto.AttackView(attacker_id="a", target_id="c")]
    assert a.you.tokens == 0 and c.you.tokens == 0
    assert "streak" not in [k for p in a.model_dump()["players"] for k in p]


def test_state_in_the_attack_window_shows_your_own_tokens_and_whether_you_acted():
    g = Game()
    g.to_attack("a", "b")
    g.send(Pass("a"))
    a = proto.state_message(g.s, "a", g.texts)
    b = proto.state_message(g.s, "b", g.texts)
    c = proto.state_message(g.s, "c", g.texts)
    assert a.you.acted and a.you.tokens == 1
    assert not b.you.acted and b.you.tokens == 1
    assert not c.you.acted and c.you.tokens == 0
    # nothing in c's snapshot says who holds a token or who passed
    dumped = c.model_dump()
    assert dumped["you"] == {"tokens": 0, "streak": 0, "answered": False, "acted": False}
    assert not any(k in ("passed", "passer") for _, k, _ in walk(dumped))


def test_state_at_the_end_is_the_full_reveal():
    g = Game(players=("a", "b"))
    g.start()
    g.send(Disconnect("b"))
    g.send(Tick(), at=g.now + CFG.abandon_seconds * 1000)
    st = proto.state_message(g.s, "b", g.texts)
    assert st.phase is Phase.END and st.end is not None
    assert st.end.reason == "abandoned" and st.end.tiebreak == "shared"
    assert {r.player_id: r.starting_xp for r in st.end.results} == {"a": 10, "b": 10}
    assert st.you is None  # b was dropped by the same tick that ended the game
    assert proto.state_message(g.s, "a", g.texts).you == proto.YouView(
        tokens=0, streak=0, answered=False, acted=False
    )


# ---------- inbound parsing ----------


@pytest.mark.parametrize(
    "raw, cls",
    [
        ({"type": "sync", "client_ms": 5}, proto.SyncIn),
        ({"type": "start"}, proto.StartIn),
        ({"type": "pick", "category_id": "g"}, proto.PickIn),
        ({"type": "answer", "question_id": "g-1", "option": 3}, proto.AnswerIn),
        ({"type": "attack", "target_player_id": "b"}, proto.AttackIn),
        ({"type": "pass"}, proto.PassIn),
        ({"type": "report", "question_id": "g-1", "reason": "typo"}, proto.ReportIn),
    ],
)
def test_client_messages_parse_from_dicts_and_json(raw, cls):
    assert isinstance(proto.parse_client_message(raw), cls)
    import json

    assert proto.parse_client_message(json.dumps(raw)) == proto.parse_client_message(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "nope"},
        {},
        {"type": "answer", "question_id": "g-1", "option": 4},
        {"type": "answer", "question_id": "g-1", "option": -1},
        {"type": "answer", "question_id": "g-1"},
        {"type": "pass", "player_id": "a"},  # the sender is the socket, never a field
        {"type": "pick"},
        {"type": "report", "question_id": "g-1", "reason": "bogus"},
        {"type": "report", "question_id": "g-1", "reason": "typo", "note": "x" * 1001},
        "not json",
    ],
)
def test_malformed_client_messages_are_rejected(raw):
    with pytest.raises(ValidationError):
        proto.parse_client_message(raw)


def test_to_event_maps_game_messages_and_skips_the_rest():
    pid = "a"
    assert proto.to_event(proto.StartIn(type="start"), pid) == Start(pid)
    assert proto.to_event(proto.PickIn(type="pick", category_id="g"), pid) == Pick(pid, "g")
    assert proto.to_event(
        proto.AnswerIn(type="answer", question_id="g-1", option=2), pid, rtt_ms=120
    ) == Answer(pid, "g-1", 2, rtt_ms=120)
    assert proto.to_event(proto.AttackIn(type="attack", target_player_id="b"), pid) == Attack(pid, "b")
    assert proto.to_event(proto.PassIn(type="pass"), pid) == Pass(pid)
    assert proto.to_event(proto.SyncIn(type="sync", client_ms=1), pid) is None
    assert proto.to_event(proto.ReportIn(type="report", question_id="x", reason="typo"), pid) is None


# ---------- glue ----------


def test_texts_from_cache_keeps_only_stem_and_options():
    import uuid

    qid = uuid.uuid4()
    cache = SessionQuestionsCache.model_validate(
        {
            "session_id": uuid.uuid4(),
            "locale": "en",
            "questions": [
                {
                    "id": qid,
                    "stem": "Q?",
                    "options": ["a", "b", "c", "d"],
                    "ordinal": 0,
                    "correct_index": 2,
                    "pool": "block",
                    "difficulty": 3,
                }
            ],
        }
    )
    texts = proto.texts_from_cache(cache)
    assert texts == {str(qid): proto.QuestionText(stem="Q?", options=["a", "b", "c", "d"])}
    assert "correct" not in texts[str(qid)].model_dump_json()


def test_public_config_drops_the_starting_xp_fields_only():
    cfg = GameConfig(starting_xp_choices=(10, 12))
    public = proto.public_config(cfg)
    assert set(public) == set(cfg.summary()) - {"starting_xp_choices", "starting_xp_tiers"}
    assert public["max_players"] == cfg.max_players


def test_every_server_message_type_is_distinct_and_matches_the_spec():
    types = {
        cls.model_fields["type"].default
        for cls in proto.ServerMessage.__args__  # type: ignore[attr-defined]
    }
    assert types == {
        "sync_reply", "lobby", "phase", "board", "question", "answer_ack", "pass_ack", "reveal",
        "attacks", "block_question", "block_result", "presence", "end", "error", "state",
        "report_ack",
    }
