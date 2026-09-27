"""app.game.snapshot: the engine state, the rng and the events survive a
trip through JSON with nothing lost — checked by continuing a game from
the copy and from the original and getting the same everything."""
from __future__ import annotations

import json
import random
from dataclasses import fields

import pytest

from app.game import engine, snapshot
from app.game.config import GameConfig
from app.game.engine import Attack, GameState, Phase
from app.game.seed import engine_rng
from tests.test_game_engine import Game


def _scripted(seed: int, *, stop_after: int | None = None) -> tuple[Game, list[engine.Event], list[int]]:
    """A seeded bot plays a 4-player game through the `Game` driver; the
    events and stamps it sent, so a copy can be fed the same ones."""
    rng = random.Random(seed)
    g = Game(players=4, config=GameConfig(min_players=2, question_count=5), rng=engine_rng(seed))
    events: list[engine.Event] = []
    stamps: list[int] = []
    original = g.send

    def send(event, at=None):
        msgs = original(event, at)
        events.append(event)
        stamps.append(g.now)
        return msgs

    g.send = send  # type: ignore[method-assign]
    g.start()
    while g.phase is not Phase.END and (stop_after is None or len(events) < stop_after):
        s = g.state
        if s.phase is Phase.PICK:
            g.pick(rng.choice(s.board))
        elif s.phase is Phase.QUESTION:
            for p in s.active_players():
                if rng.random() < 0.8:
                    g.answer(p.id, rng.random() < 0.6, after_ms=rng.randint(200, 3000), rtt_ms=rng.randint(0, 900))
            if g.phase is Phase.QUESTION:
                g.tick()
        elif s.phase is Phase.ATTACK:
            for p in s.active_players():
                if s.awaiting_attack(p.id) and rng.random() < 0.9:
                    g.send(Attack(p.id, rng.choice([q.id for q in s.active_players() if q.id != p.id])))
            if g.phase is Phase.ATTACK:
                g.tick()
        elif s.phase is Phase.BLOCK:
            for pid in list(s.blocks):
                if rng.random() < 0.7:
                    g.block_answer(pid, rng.random() < 0.5)
            if g.phase is Phase.BLOCK:
                g.tick()
        else:
            g.tick()
    return g, events, stamps


def _round_trip(state: GameState, rng: random.Random) -> snapshot.Snapshot:
    raw = snapshot.dump(state, rng, seq=42, shown={"p0": {"geo-1", "geo-0"}}, saved_ms=5)
    json.loads(raw)  # plain JSON
    return snapshot.load(raw)


@pytest.mark.parametrize("stop_after", [0, 3, 9, 17, 31, None])
def test_state_and_rng_survive_the_round_trip(stop_after):
    g, events, _ = _scripted(11, stop_after=stop_after)
    snap = _round_trip(g.state, g.rng)
    assert snap.state == g.state
    assert snap.state is not g.state
    assert snap.seq == 42 and snap.saved_ms == 5 and snap.shown == {"p0": {"geo-0", "geo-1"}}
    assert snap.rng.getstate() == g.rng.getstate()
    # Questions are rebuilt as equal frozen values, not shared objects.
    if g.state.question is not None:
        assert snap.state.question == g.state.question


def test_every_field_of_the_state_is_covered():
    """A field added to GameState (or Player) must reach the snapshot:
    the dump names each one."""
    d = snapshot.dump_state(Game().state)
    assert set(d) == {f.name for f in fields(GameState)}
    assert set(d["players"]["p0"]) == {f.name for f in fields(engine.Player)}


def test_a_game_continued_from_the_copy_matches_the_original():
    """Stop mid-game, snapshot, then feed the same remaining events (with
    their stamps) to both: identical messages and identical end state."""
    full, events, stamps = _scripted(5)
    assert full.phase is Phase.END
    part, part_events, _ = _scripted(5, stop_after=23)
    cut = len(part_events)
    assert part_events == events[:cut] and len(part.log) < len(full.log)
    snap = _round_trip(part.state, part.rng)

    state, rng, out = snap.state, snap.rng, []
    for event, at in zip(events[cut:], stamps[cut:]):
        state, msgs = engine.step(state, event, at, rng)
        out.extend(msgs)
    assert state == full.state
    assert state.phase is Phase.END and state.results is not None
    assert out == full.log[len(part.log):]


def test_mid_phase_details_survive():
    """The awkward bits: pending answers with response times, a block
    challenge with its question, the acted set, absent players."""
    g = Game(players=4, config=GameConfig(min_players=2, starting_xp_choices=(10,)), rng=engine_rng(3))
    g.start()
    g.earn_token("p0", others=("p1",))
    g.play_question({"p0": True, "p1": False})
    g.past_reveal()
    assert g.phase is Phase.ATTACK
    g.send(Attack("p0", "p2"))
    g.send(Attack("p1", "p3"))  # every token holder attacked: BLOCK opens early
    g.send(engine.Disconnect("p3"))
    assert g.phase is Phase.BLOCK and set(g.state.blocks) == {"p2", "p3"}
    g.block_answer("p2", right=True)
    assert g.phase is Phase.BLOCK  # p3 still owes an answer
    snap = _round_trip(g.state, g.rng)
    s = snap.state
    assert s == g.state
    assert s.blocks["p2"].answer is not None and s.blocks["p2"].question is not None
    assert s.blocks["p3"].answer is None
    assert not s.players["p3"].present and s.players["p3"].absent_since_ms is not None
    assert "p0" in s.acted_this_window


def test_config_overrides_survive():
    cfg = GameConfig.from_overrides(
        {"starting_xp_choices": [7, 9], "starting_xp_tiers": [[4, [1, 2]], [9, [3]]], "grace_max_ms": 2000}
    )
    g = Game(config=cfg)
    assert snapshot.load_state(json.loads(json.dumps(snapshot.dump_state(g.state)))).config == cfg


def test_a_snapshot_of_another_version_is_refused():
    raw = snapshot.dump(Game().state, engine_rng(1), 0, {}, 0)
    d = json.loads(raw)
    d["v"] = snapshot.VERSION + 1
    with pytest.raises(ValueError, match="version"):
        snapshot.load(json.dumps(d))


@pytest.mark.parametrize(
    "event",
    [
        engine.Join("p9", "Nine", as_host=True),
        engine.Start("p0"),
        engine.Pick("p0", "geo"),
        engine.Answer("p1", "geo-2", 3, rtt_ms=120),
        engine.Attack("p0", "p1"),
        engine.Pass("p1"),
        engine.Disconnect("p2"),
        engine.Reconnect("p2"),
        engine.Tick(),
    ],
)
def test_events_round_trip_as_kind_and_payload(event):
    kind, payload = snapshot.encode_event(event)
    assert kind == type(event).__name__.lower()
    payload = json.loads(json.dumps(payload))
    assert snapshot.decode_event(kind, payload) == event


def test_unknown_event_kind_is_refused():
    with pytest.raises(ValueError, match="unknown event kind"):
        snapshot.decode_event("teleport", {})
