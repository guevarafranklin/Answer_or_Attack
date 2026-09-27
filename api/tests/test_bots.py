"""The §8 bots' bookkeeping (scripts/bots.py), driven with hand-made server
messages: the simulated network, on-time/late answer accounting,
phase-transition lag from server stamps, attack policies, and the target
verdicts. The bots themselves run against a real API (see the script)."""
from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bots  # noqa: E402

CONFIG = {
    "pick_seconds": 8,
    "question_seconds": 10,
    "reveal_seconds": 4,
    "attack_window_seconds": 6,
    "block_seconds": 5,
    "grace_base_ms": 200,
    "grace_min_ms": 400,
    "grace_max_ms": 1000,
}


def make_bot(**kw: Any) -> tuple[bots.Bot, list[dict[str, Any]]]:
    game = SimpleNamespace(run=SimpleNamespace(answer_key={"q1": "B", "q2": "D"}), code="ABCDEF")
    params = dict(
        game=game, name="b", accuracy=1.0, profile=0, delay=lambda rng: 0.0, attack="greedy", rng=random.Random(1)
    )
    params.update(kw)
    bot = bots.Bot(**params)
    bot.player_id = "me"
    sent: list[dict[str, Any]] = []

    async def capture(m: dict[str, Any]) -> None:
        sent.append(m)

    bot._raw_send = capture  # type: ignore[method-assign]
    return bot, sent


def question(qid: str, options: list[str], deadline_ms: int) -> dict[str, Any]:
    return {"type": "question", "question_id": qid, "stem": "?", "options": options, "deadline_ms": deadline_ms}


async def settle(bot: bots.Bot) -> None:
    """Let the bot's scheduled taps run (their delays are 0 in these tests)."""
    for _ in range(3):
        await asyncio.sleep(0)
    await asyncio.gather(*bot.tasks, return_exceptions=True)


def test_delay_specs():
    rng = random.Random(0)
    normal = bots.parse_delay("3500±1500")
    xs = [normal(rng) for _ in range(2000)]
    assert min(xs) >= 100 and 3300 < sum(xs) / len(xs) < 3700
    assert bots.parse_delay("3500+-1500") is not None
    uniform = bots.parse_delay("500-9900")
    ys = [uniform(rng) for _ in range(2000)]
    assert 500 <= min(ys) and max(ys) <= 9900 and 4800 < sum(ys) / len(ys) < 5600
    assert bots.parse_delay("4000")(rng) == 4000
    with pytest.raises(ValueError):
        bots.parse_delay("fast")


def test_cpu_time_parsing():
    assert bots.CpuSampler._seconds("0:03.45") == pytest.approx(3.45)
    assert bots.CpuSampler._seconds("1:02:03") == pytest.approx(3723)


@pytest.mark.asyncio
async def test_wire_delays_in_order_within_the_profile():
    got: list[tuple[int, float]] = []
    loop = asyncio.get_running_loop()

    async def deliver(i: int) -> None:
        got.append((i, loop.time()))

    wire = bots.Wire(150, 50, random.Random(3), deliver)
    task = asyncio.create_task(wire.run())
    t0 = loop.time()
    for i in range(20):
        wire.put(i)
    await asyncio.sleep(0.35)
    task.cancel()
    assert [i for i, _ in got] == list(range(20))  # nothing overtakes
    delays = [(t - t0) * 1000 for _, t in got]
    assert all(95 <= d <= 260 for d in delays), delays  # 150±50, plus scheduling slack
    assert delays == sorted(delays)


@pytest.mark.asyncio
async def test_on_time_answers_and_their_verdicts():
    bot, sent = make_bot()
    await bot.on_message({"type": "lobby", "players": [{"player_id": "me"}], "host_id": "me", "config": CONFIG})
    now = bot.server_now()
    await bot.on_message({"type": "phase", "phase": "question", "round": 1, "deadline_ms": int(now) + 5_000})
    await bot.on_message(question("q1", ["A", "B", "C", "D"], int(now) + 5_000))
    await settle(bot)
    assert sent[-1] == {"type": "answer", "question_id": "q1", "option": 1}  # accuracy 1: the correct one
    a = bot.answers[-1]
    assert a.kind == "question" and a.on_time and 4_900 <= a.countdown_ms <= 5_000 and a.accepted is None
    await bot.on_message({"type": "answer_ack", "question_id": "q1", "accepted": True, "reason": None})
    assert a.accepted is True and a.how == "ack" and not bot.pending

    # A tap after the countdown hit zero is late; the ack says so.
    await bot.on_message({"type": "phase", "phase": "question", "round": 2, "deadline_ms": int(bot.server_now()) - 100})
    await bot.on_message(question("q2", ["D", "A", "B", "C"], int(bot.server_now()) - 100))
    await settle(bot)
    late = bot.answers[-1]
    assert not late.on_time and late.countdown_ms <= -100 and sent[-1]["option"] == 0
    await bot.on_message({"type": "answer_ack", "question_id": "q2", "accepted": False, "reason": "too_late"})
    assert late.accepted is False and late.how == "ack:too_late"

    # An answer the server never acknowledged: `wrong_phase`, or a reveal
    # that scored a timeout, or the game ending — each counts as rejected.
    for how, m in (
        ("error:wrong_phase", {"type": "error", "code": "wrong_phase", "message": "no question is open"}),
        ("timeout_reveal", {"type": "reveal", "question_id": "q1", "correct_option": 1, "outcome": "timeout",
                            "points": -1, "delta": -1, "streak": 0, "tokens": 0, "token_earned": False, "deltas": {}}),
        ("unanswered", {"type": "end", "reason": "finished", "results": [], "winner_ids": [], "tiebreak": None}),
    ):
        pending = bots.Answer("question", "q1", True, 3_000)
        bot.answers.append(pending)
        bot.pending["q1"] = pending
        await bot.on_message(m)
        assert pending.accepted is False and pending.how == how, how


@pytest.mark.asyncio
async def test_the_phase_moving_on_before_the_tap_is_overtaken_not_answered():
    bot, sent = make_bot(delay=lambda rng: 50.0)
    await bot.on_message({"type": "lobby", "players": [], "host_id": "me", "config": CONFIG})
    now = int(bot.server_now())
    await bot.on_message(question("q1", ["A", "B", "C", "D"], now + 5_000))
    await bot.on_message({"type": "phase", "phase": "reveal", "round": 1, "deadline_ms": now + 4_000})
    await settle(bot)
    assert bot.overtaken == 1 and not bot.answers and not [m for m in sent if m["type"] == "answer"]


@pytest.mark.asyncio
async def test_accuracy_picks_a_wrong_option_the_rest_of_the_time():
    bot, sent = make_bot(accuracy=0.0)
    await bot.on_message({"type": "lobby", "players": [], "host_id": "me", "config": CONFIG})
    for _ in range(20):
        now = int(bot.server_now())
        await bot.on_message(question("q1", ["C", "A", "B", "D"], now + 5_000))
        await settle(bot)
        bot.pending.clear()
    taps = {m["option"] for m in sent if m["type"] == "answer"}
    assert taps and 2 not in taps  # "B" is shown at 2 and is never tapped


@pytest.mark.asyncio
async def test_transition_lag_comes_from_server_stamps():
    bot, _ = make_bot()
    await bot.on_message({"type": "lobby", "players": [], "host_id": "me", "config": CONFIG})
    t0 = int(bot.server_now())
    # QUESTION: deadline t0+10000, phase_end t0+11000. It ends early (everyone
    # answered) at t0+6000: REVEAL's start is before the scheduled end -> no lag sample.
    await bot.on_message({"type": "phase", "phase": "question", "round": 1, "deadline_ms": t0 + 10_000})
    await bot.on_message({"type": "phase", "phase": "reveal", "round": 1, "deadline_ms": t0 + 6_000 + 4_000})
    assert bot.transitions == []
    # REVEAL has no grace: scheduled end t0+10000. ATTACK starts at t0+10037
    # (deadline t0+16037): the timer fired 37 ms late.
    await bot.on_message({"type": "phase", "phase": "attack", "round": 1, "deadline_ms": t0 + 16_037})
    assert [(t.phase, t.tick_lag_ms) for t in bot.transitions] == [("attack", 37)]
    arrival = bot.transitions[0].arrival_lag_ms
    assert arrival >= -10_000  # measured against the same scheduled end, on the bot's synced clock
    # ATTACK's end includes grace_max: t0+17037. BLOCK begins right then.
    await bot.on_message({"type": "phase", "phase": "block", "round": 1, "deadline_ms": t0 + 17_037 + 5_000})
    assert bot.transitions[-1].tick_lag_ms == 0
    # END has no timer: nothing measured, and nothing to measure the next one against.
    await bot.on_message({"type": "phase", "phase": "end", "round": 1, "deadline_ms": None})
    assert len(bot.transitions) == 2 and bot.prev_phase is None


@pytest.mark.asyncio
async def test_attack_policies():
    players = [{"player_id": p} for p in ("me", "p1", "p2", "p3")]
    reveal = {"type": "reveal", "question_id": "q1", "correct_option": 0, "outcome": "correct", "points": 3,
              "delta": 3, "streak": 2, "tokens": 1, "token_earned": True, "deltas": {"me": 3, "p1": 5, "p2": 9, "p3": -2}}
    # greedy: the highest delta first, then the next when the target is full.
    bot, sent = make_bot(attack="greedy", rng=random.Random(0))
    bot.rng.uniform = lambda a, b: 0.0  # type: ignore[method-assign] - no think time
    await bot.on_message({"type": "lobby", "players": players, "host_id": "me", "config": CONFIG})
    await bot.on_message(reveal)
    await bot.on_message({"type": "phase", "phase": "attack", "round": 1, "deadline_ms": 1})
    await settle(bot)
    assert sent[-1] == {"type": "attack", "target_player_id": "p2"}
    await bot.on_message({"type": "error", "code": "target_full", "message": "full"})
    assert sent[-1] == {"type": "attack", "target_player_id": "p1"} and bot.attacks_made == 2
    # never: holders pass so the window closes.
    bot, sent = make_bot(attack="never")
    bot.rng.uniform = lambda a, b: 0.0  # type: ignore[method-assign]
    await bot.on_message({"type": "lobby", "players": players, "host_id": "me", "config": CONFIG})
    await bot.on_message(reveal)
    await bot.on_message({"type": "phase", "phase": "attack", "round": 1, "deadline_ms": 1})
    await settle(bot)
    assert sent[-1] == {"type": "pass"} and bot.passes == 1
    # random: one of the others; without a token, nothing at all.
    bot, sent = make_bot(attack="random")
    bot.rng.uniform = lambda a, b: 0.0  # type: ignore[method-assign]
    await bot.on_message({"type": "lobby", "players": players, "host_id": "me", "config": CONFIG})
    await bot.on_message({"type": "phase", "phase": "attack", "round": 1, "deadline_ms": 1})
    await settle(bot)
    assert not sent
    await bot.on_message(reveal)
    await bot.on_message({"type": "phase", "phase": "attack", "round": 1, "deadline_ms": 1})
    await settle(bot)
    assert sent[-1]["type"] == "attack" and sent[-1]["target_player_id"] in {"p1", "p2", "p3"}


def test_target_verdicts():
    def rep(
        rejected: int,
        on_time: int,
        tick: float,
        arrival: float,
        grace: tuple[int, int, int] = (200, 400, 1000),
        bots_n: int = 20,
        sessions: int = 10,
    ):
        return {
            "config": dict(zip(("grace_base_ms", "grace_min_ms", "grace_max_ms"), grace)),
            "args": {"bots": bots_n, "sessions": sessions},
            "tick_lag": {"mean_ms": tick, "max_ms": tick, "count": 1},
            "profiles": {
                800: {"on_time": on_time, "on_time_rejected": rejected},
                0: {"arrival_lag_mean_ms": arrival},
            },
        }

    lines = bots.targets(rep(1, 100, 2.0, 30.0))
    assert lines[0].startswith("[PASS]".join(("  ", " 800±300: 1 of 100"))) and "1.00%" in lines[0]
    assert lines[1].startswith("  [PASS] transition lag") and "the target is 20×10" not in lines[1]
    lines = bots.targets(rep(3, 100, 2.0, 120.0, grace=(0, 900, 900), bots_n=4, sessions=1))
    assert lines[0].startswith("  [FAIL] 800±300") and "0/900/900, not the default 200/400/1000" in lines[0]
    assert lines[1].startswith("  [FAIL] transition lag") and "measured at 4×1" in lines[1]
    empty = bots.targets({"config": {}, "args": {"bots": 1, "sessions": 1}, "tick_lag": {"mean_ms": None}, "profiles": {}})
    assert all(line.startswith("  [skip]") for line in empty)
