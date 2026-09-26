"""The pure engine against every rule in spec §2 (build step 1, §9 first
checkbox): scoring and the XP floor, streaks and tokens, attacks with the
cost / incoming cap / both §2.5 amendments, blocks, presence, the winner
tiebreaks — plus the §1 purity guarantees (no clock, no RNG, no mutation
of the input state, deterministic replay).

`Game` below drives a session the way the runtime will: it stamps server
time on every event and ticks at `phase_end_ms`.
"""
from __future__ import annotations

import copy
import random
from dataclasses import fields
from typing import Any

import pytest

from app.game import engine
from app.game.config import GameConfig
from app.game.engine import (
    Answer,
    AnswerAck,
    Attack,
    AttackDeclared,
    BlockQuestionShown,
    BlockResolved,
    BoardShown,
    Disconnect,
    Ended,
    Error,
    GameState,
    HostChanged,
    Join,
    Pass,
    Passed,
    Phase,
    PhaseChanged,
    Pick,
    PlayerJoined,
    PresenceChanged,
    Question,
    QuestionShown,
    Reconnect,
    Revealed,
    Start,
    Tick,
    new_game,
    step,
)

T0 = 1_700_000_000_000  # some epoch ms
CATS = ("geo", "hist", "sci")

# Everyone starts at 10 unless a test says otherwise, so deltas and totals
# are easy to reason about; the multi-choice draw is tested on its own.
# rejoin_seconds is shorter than any phase so a tick that drops a player
# never also crosses a phase deadline.
CFG = GameConfig(starting_xp_choices=(10,), min_players=2, rejoin_seconds=2)
# For tests where someone is absent across whole rounds and comes back.
LONG_REJOIN = GameConfig.from_overrides(CFG.summary(), rejoin_seconds=600)


def make_pools(cats=CATS, per_cat=8) -> dict[str, list[Question]]:
    """Ramped pools: question i of a category has difficulty 1 + i // 3 and
    correct option i % 4, so a test can read the answer off the id."""
    return {
        c: [Question(f"{c}-{i}", c, 1 + i // 3, i % 4) for i in range(per_cat)] for c in cats
    }


def make_reserve(n=6) -> list[Question]:
    return [Question(f"blk-{i}", "hist", 2 + i % 2, i % 4) for i in range(n)]


class ScriptedRng:
    """An RNG whose choices are known in advance (starting XP, auto-pick),
    falling back to a seeded random.Random for board sampling."""

    def __init__(self, choices: list[Any] | None = None, seed: int = 0):
        self.choices = list(choices or [])
        self.inner = random.Random(seed)
        self.choice_calls: list[Any] = []

    def choice(self, seq):
        self.choice_calls.append(tuple(seq))
        if self.choices:
            return self.choices.pop(0)
        return self.inner.choice(seq)

    def sample(self, population, k):
        return self.inner.sample(population, k)


class Game:
    """Test driver: holds state, server time and the RNG; sends events and
    ticks at deadlines the way the runtime will."""

    def __init__(
        self,
        players: int = 3,
        config: GameConfig = CFG,
        pools: dict[str, list[Question]] | None = None,
        reserve: list[Question] | None = None,
        rng: Any = None,
        join: bool = True,
    ):
        self.cfg = config
        self.state = new_game(
            config,
            pools if pools is not None else make_pools(),
            reserve if reserve is not None else make_reserve(),
        )
        self.now = T0
        self.rng = rng or ScriptedRng()
        self.log: list[engine.Message] = []
        if join:
            for i in range(players):
                self.send(Join(f"p{i}", f"Player {i}"))

    # ----- plumbing -----

    def send(self, event: engine.Event, at: int | None = None) -> list[engine.Message]:
        if at is not None:
            self.now = at
        self.state, msgs = step(self.state, event, self.now, self.rng)
        self.log.extend(msgs)
        return msgs

    def tick(self, at: int | None = None) -> list[engine.Message]:
        """Tick at the phase end (or at `at`)."""
        if at is None:
            at = self.state.phase_end_ms
            assert at is not None
        return self.send(Tick(), at=max(self.now, at))

    def player(self, pid: str) -> engine.Player:
        return self.state.players[pid]

    @property
    def phase(self) -> Phase:
        return self.state.phase

    # ----- game actions -----

    def start(self, by: str = "p0") -> list[engine.Message]:
        return self.send(Start(by))

    def pick(self, category: str | None = None) -> list[engine.Message]:
        assert self.phase is Phase.PICK, self.phase
        assert self.state.picker_id is not None
        return self.send(Pick(self.state.picker_id, category or self.state.board[0]))

    def correct(self) -> int:
        assert self.state.question is not None
        return self.state.question.correct_option

    def wrong(self) -> int:
        return (self.correct() + 1) % 4

    def answer(self, pid: str, right: bool = True, *, after_ms: int = 100, rtt_ms: int = 0):
        """Answer the open round question `after_ms` after it was shown."""
        assert self.state.question is not None
        option = self.correct() if right else self.wrong()
        return self.send(
            Answer(pid, self.state.question.id, option, rtt_ms=rtt_ms),
            at=self.state.question_sent_ms + after_ms,
        )

    def block_answer(self, pid: str, right: bool = True, *, after_ms: int = 100):
        block = self.state.blocks[pid]
        assert block.question is not None
        option = block.question.correct_option if right else (block.question.correct_option + 1) % 4
        return self.send(Answer(pid, block.question.id, option), at=self.now + after_ms)

    def play_question(self, answers: dict[str, bool | None]) -> Revealed:
        """From PICK: pick, have each listed player answer (True/False; None
        = no answer), tick to the reveal. Returns the Revealed message."""
        self.pick()
        for pid, right in answers.items():
            if right is not None:
                self.answer(pid, right)
        if self.phase is Phase.QUESTION:
            self.tick()
        assert self.phase is Phase.REVEAL, self.phase
        return one(self.log_since_last_pick(), Revealed)

    def log_since_last_pick(self) -> list[engine.Message]:
        idx = max(i for i, m in enumerate(self.log) if isinstance(m, BoardShown))
        return self.log[idx:]

    def past_reveal(self) -> None:
        """Tick out of REVEAL (into ATTACK or the next PICK)."""
        assert self.phase is Phase.REVEAL
        self.tick()

    def round_trip(self, answers: dict[str, bool | None]) -> None:
        """A whole round with no attacks: PICK → … → next PICK/END."""
        self.play_question(answers)
        self.past_reveal()
        if self.phase is Phase.ATTACK:
            self.tick()
        assert self.phase in (Phase.PICK, Phase.END), self.phase

    def earn_token(self, pid: str, others: tuple[str, ...] = ()) -> None:
        """Play rounds until `pid` holds a token; `others` answer correctly
        too (so they keep pace), everyone else stays quiet (timeout)."""
        while self.player(pid).tokens == 0:
            answers = {pid: True, **{o: True for o in others}}
            self.round_trip(answers)


def one(msgs: list[engine.Message], kind: type) -> Any:
    found = [m for m in msgs if isinstance(m, kind)]
    assert len(found) == 1, f"expected one {kind.__name__}, got {found}"
    return found[0]


def errors(msgs: list[engine.Message]) -> list[str]:
    return [m.code for m in msgs if isinstance(m, Error)]


def phases(msgs: list[engine.Message]) -> list[Phase]:
    return [m.phase for m in msgs if isinstance(m, PhaseChanged)]


# =====================================================================
# §1 purity
# =====================================================================


def test_engine_has_no_clock_and_no_randomness_of_its_own():
    """No time/datetime/random anywhere in the engine module."""
    names = set(vars(engine))
    assert not names & {"time", "datetime", "random", "secrets", "uuid", "os"}


def test_step_leaves_the_input_state_untouched():
    g = Game()
    before = copy.deepcopy(g.state)
    state, _ = step(g.state, Start("p0"), T0, ScriptedRng())
    assert state.phase is Phase.PICK
    assert g.state == before
    assert g.state.phase is Phase.LOBBY


def test_step_can_mutate_in_place_for_the_simulator():
    g = Game()
    state, _ = step(g.state, Start("p0"), T0, ScriptedRng(), copy_state=False)
    assert state is g.state
    assert g.state.phase is Phase.PICK


def test_questions_are_shared_between_state_copies():
    g = Game()
    state, _ = step(g.state, Start("p0"), T0, ScriptedRng())
    assert state.pools["geo"][0] is g.state.pools["geo"][0]
    assert state.pools["geo"] is not g.state.pools["geo"]


def test_unknown_event_type_is_rejected():
    with pytest.raises(TypeError):
        step(Game().state, "start", T0, ScriptedRng())  # type: ignore[arg-type]


def _scripted_game(seed: int) -> tuple[GameState, list[engine.Message]]:
    """A full game driven by a seeded bot: same seed → same everything."""
    rng = random.Random(seed)
    g = Game(players=4, config=GameConfig(min_players=2, question_count=6), rng=random.Random(seed))
    log: list[engine.Message] = []
    g.start()
    guard = 0
    while g.phase is not Phase.END:
        guard += 1
        assert guard < 500
        s = g.state
        if s.phase is Phase.PICK:
            g.pick(rng.choice(s.board))
        elif s.phase is Phase.QUESTION:
            for p in s.active_players():
                if rng.random() < 0.8:
                    g.answer(p.id, rng.random() < 0.6, after_ms=rng.randint(200, 3000))
            if g.phase is Phase.QUESTION:
                g.tick()
        elif s.phase is Phase.ATTACK:
            for p in s.active_players():
                if s.awaiting_attack(p.id) and rng.random() < 0.7:
                    target = rng.choice([q.id for q in s.active_players() if q.id != p.id])
                    g.send(Attack(p.id, target))
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
        log = g.log
    return g.state, log


def test_replay_with_the_same_seed_is_identical():
    s1, log1 = _scripted_game(7)
    s2, log2 = _scripted_game(7)
    assert log1 == log2
    assert s1 == s2
    assert s1.phase is Phase.END
    assert s1.results is not None


def test_different_seeds_diverge():
    s1, _ = _scripted_game(1)
    s2, _ = _scripted_game(2)
    assert s1.serves != s2.serves


# =====================================================================
# Lobby: join, host, start
# =====================================================================


def test_first_joiner_is_host_and_joins_are_announced():
    g = Game(join=False)
    msgs = g.send(Join("a", "Ann"))
    assert one(msgs, PlayerJoined).player_id == "a"
    assert one(msgs, HostChanged).host_id == "a"
    msgs = g.send(Join("b", "Bob"))
    assert not [m for m in msgs if isinstance(m, HostChanged)]
    assert g.state.host_id == "a"
    assert list(g.state.players) == ["a", "b"]


def test_explicit_host_join_takes_the_host_seat():
    g = Game(join=False)
    g.send(Join("a", "Ann"))
    g.send(Join("h", "Host", as_host=True))
    assert g.state.host_id == "h"


def test_join_refused_when_full_or_duplicate_or_started():
    g = Game(players=2, config=GameConfig.from_overrides(CFG.summary(), max_players=2))
    assert errors(g.send(Join("p9", "Late"))) == ["session_full"]
    assert errors(g.send(Join("p0", "Again"))) == ["already_joined"]
    g.start()
    assert errors(g.send(Join("p9", "Late"))) == ["wrong_phase"]
    assert "p9" not in g.state.players


def test_only_the_host_can_start_and_only_once():
    g = Game()
    assert errors(g.start(by="p1")) == ["not_host"]
    assert g.phase is Phase.LOBBY
    assert errors(g.send(Start("nobody"))) == ["unknown_player"]
    g.start()
    assert g.phase is Phase.PICK
    assert errors(g.start()) == ["wrong_phase"]


def test_start_needs_min_players_present():
    g = Game(players=2, config=GameConfig.from_overrides(CFG.summary(), min_players=4))
    assert errors(g.start()) == ["not_enough_players"]
    g = Game(players=2)
    g.send(Disconnect("p1"))
    assert errors(g.start()) == ["not_enough_players"]  # absent players don't count


def test_start_needs_questions():
    g = Game(pools={"geo": []})
    assert errors(g.start()) == ["no_questions"]


def test_start_draws_secret_starting_xp_from_the_choices():
    cfg = GameConfig(min_players=2)  # (10, 18, 30)
    rng = ScriptedRng(choices=[30, 10, 18])
    g = Game(players=3, config=cfg, rng=rng)
    g.start()
    assert [g.player(p).starting_xp for p in ("p0", "p1", "p2")] == [30, 10, 18]
    assert all(p.xp == p.starting_xp and p.delta == 0 for p in g.state.players.values())
    # each draw was over exactly the configured choices
    assert rng.choice_calls[:3] == [cfg.starting_xp_choices] * 3


def test_start_drops_players_who_left_the_lobby_for_good():
    g = Game(players=3)
    g.send(Disconnect("p2"))
    g.tick(at=T0 + CFG.rejoin_seconds * 1000)
    assert g.player("p2").dropped
    g.start()
    assert list(g.state.players) == ["p0", "p1"]


# =====================================================================
# §2.2 round flow and timing (§5 grace)
# =====================================================================


def test_start_opens_round_one_pick_with_a_board():
    g = Game()
    msgs = g.start()
    assert g.phase is Phase.PICK
    assert g.state.round == 1
    ph = one(msgs, PhaseChanged)
    assert (ph.phase, ph.round) == (Phase.PICK, 1)
    assert g.state.deadline_ms == T0 + CFG.pick_seconds * 1000
    assert g.state.phase_end_ms == g.state.deadline_ms + CFG.grace_ms
    board = one(msgs, BoardShown)
    assert board.picker_id == "p0"
    assert set(board.category_ids) == set(CATS)  # 3 categories < board_size 4
    assert len(set(board.category_ids)) == len(board.category_ids)


def test_board_is_capped_at_board_size():
    cats = tuple(f"c{i}" for i in range(7))
    g = Game(pools=make_pools(cats))
    g.start()
    assert len(g.state.board) == CFG.board_size
    assert set(g.state.board) <= set(cats)


def test_board_only_offers_categories_with_questions_left():
    pools = make_pools(per_cat=1)
    pools["sci"] = []
    g = Game(pools=pools)
    g.start()
    assert set(g.state.board) == {"geo", "hist"}


def test_pick_shows_the_next_question_of_that_category():
    g = Game()
    g.start()
    msgs = g.pick("hist")
    assert g.phase is Phase.QUESTION
    shown = one(msgs, QuestionShown)
    assert (shown.question_id, shown.category_id) == ("hist-0", "hist")
    assert shown.deadline_ms == T0 + CFG.question_seconds * 1000
    assert g.state.question_sent_ms == T0
    assert g.state.phase_end_ms == shown.deadline_ms + CFG.grace_ms
    assert [q.id for q in g.state.pools["hist"]] == [f"hist-{i}" for i in range(1, 8)]


def test_pools_are_served_easy_to_hard():
    g = Game()
    g.start()
    difficulties = []
    for _ in range(6):
        g.pick("geo")
        difficulties.append(g.state.question.difficulty)
        g.tick()  # question deadline
        g.past_reveal()
    assert difficulties == sorted(difficulties) == [1, 1, 1, 2, 2, 2]


def test_pick_validation():
    g = Game()
    assert errors(g.send(Pick("p0", "geo"))) == ["wrong_phase"]
    g.start()
    assert errors(g.send(Pick("p1", "geo"))) == ["not_your_pick"]
    assert errors(g.send(Pick("p0", "math"))) == ["not_on_board"]
    assert errors(g.send(Pick("zz", "geo"))) == ["unknown_player"]
    assert g.phase is Phase.PICK


def test_pick_after_the_grace_is_too_late():
    g = Game()
    g.start()
    late = g.state.phase_end_ms + 1
    assert errors(g.send(Pick("p0", "geo"), at=late)) == ["too_late"]
    assert g.phase is Phase.PICK


def test_pick_within_grace_is_accepted():
    g = Game()
    g.start()
    g.send(Pick("p0", "geo"), at=g.state.phase_end_ms)
    assert g.phase is Phase.QUESTION


def test_auto_pick_on_timeout_uses_the_rng_over_the_board():
    rng = ScriptedRng(choices=[10, 10, 10, "sci"])
    g = Game(rng=rng)
    g.start()
    board = tuple(g.state.board)
    msgs = g.tick()
    assert g.phase is Phase.QUESTION
    assert one(msgs, QuestionShown).category_id == "sci"
    assert rng.choice_calls[-1] == board


def test_tick_before_the_phase_end_does_nothing():
    g = Game()
    g.start()
    before = copy.deepcopy(g.state)
    assert g.tick(at=g.state.phase_end_ms - 1) == []
    assert g.state == before


def test_tick_in_lobby_and_after_end_is_harmless():
    g = Game()
    assert g.tick(at=T0 + 10**9) == []
    g.start()
    for _ in range(CFG.question_count):
        g.round_trip({})
    assert g.phase is Phase.END
    assert g.tick(at=T0 + 10**9) == []


def test_reveal_has_no_grace_but_input_phases_do():
    g = Game()
    g.start()
    g.play_question({"p0": True, "p1": True})
    assert g.phase is Phase.REVEAL
    assert g.state.deadline_ms == g.now + CFG.reveal_seconds * 1000
    assert g.state.phase_end_ms == g.state.deadline_ms
    g.past_reveal()
    g.earn_token("p0")
    g.play_question({"p0": True})
    g.past_reveal()
    assert g.phase is Phase.ATTACK
    assert g.state.phase_end_ms == g.state.deadline_ms + CFG.grace_ms
    g.send(Attack("p0", "p1"))
    assert g.phase is Phase.BLOCK
    assert g.state.deadline_ms == g.now + CFG.block_seconds * 1000
    assert g.state.phase_end_ms == g.state.deadline_ms + CFG.grace_ms


def test_full_round_phase_sequence_without_attacks():
    g = Game()
    g.start()
    g.pick()
    g.answer("p0")
    g.tick()  # question deadline
    g.tick()  # reveal → nobody can attack → next PICK
    assert phases(g.log) == [Phase.PICK, Phase.QUESTION, Phase.REVEAL, Phase.PICK]
    assert g.state.round == 2


def test_game_ends_after_question_count_rounds():
    cfg = GameConfig.from_overrides(CFG.summary(), question_count=3)
    g = Game(config=cfg)
    g.start()
    for r in (1, 2, 3):
        assert g.state.round == r
        g.round_trip({"p0": True})
    assert g.phase is Phase.END
    ended = one(g.log, Ended)
    assert ended.reason == "finished"
    assert g.state.end_reason == "finished"
    assert g.state.round == 3
    assert g.state.deadline_ms is None and g.state.phase_end_ms is None


def test_game_ends_early_when_every_pool_is_empty():
    g = Game(pools=make_pools(per_cat=1))  # 3 questions total, question_count 15
    g.start()
    for _ in range(3):
        g.round_trip({})
    assert g.phase is Phase.END
    assert one(g.log, Ended).reason == "finished"
    assert g.state.round == 3


def test_picker_rotates_in_join_order():
    g = Game(players=3)
    g.start()
    pickers = []
    for _ in range(5):
        pickers.append(g.state.picker_id)
        g.round_trip({})
    assert pickers == ["p0", "p1", "p2", "p0", "p1"]


# =====================================================================
# §2.3 scoring, XP floor, streaks; §2.4 tokens
# =====================================================================


def test_correct_wrong_and_timeout_points():
    g = Game(players=3)
    g.start()
    rev = g.play_question({"p0": True, "p1": False, "p2": None})
    assert rev.correct_option == g.state.question.correct_option
    assert (rev.outcomes["p0"].outcome, rev.outcomes["p0"].points) == ("correct", 3)
    assert (rev.outcomes["p1"].outcome, rev.outcomes["p1"].points) == ("incorrect", -1)
    assert (rev.outcomes["p2"].outcome, rev.outcomes["p2"].points) == ("timeout", -1)
    assert [g.player(p).xp for p in ("p0", "p1", "p2")] == [13, 9, 9]
    assert [rev.outcomes[p].delta for p in ("p0", "p1", "p2")] == [3, -1, -1]


def test_points_come_from_config():
    cfg = GameConfig.from_overrides(CFG.summary(), points_correct=5, points_wrong=-2, points_timeout=0)
    g = Game(config=cfg)
    g.start()
    g.play_question({"p0": True, "p1": False})
    assert [g.player(p).xp for p in ("p0", "p1", "p2")] == [15, 8, 10]


def test_xp_never_goes_below_zero():
    cfg = GameConfig.from_overrides(CFG.summary(), starting_xp_choices=(1,))
    g = Game(config=cfg)
    g.start()
    g.round_trip({"p0": False})
    assert g.player("p0").xp == 0
    rev = g.play_question({"p0": False})
    assert g.player("p0").xp == 0
    assert rev.outcomes["p0"].points == -1  # nominal, absorbed by the floor
    assert rev.outcomes["p0"].delta == -1
    g.past_reveal()
    g.round_trip({})  # timeout at zero
    assert g.player("p0").xp == 0
    assert g.player("p0").delta == -1


def test_streak_resets_on_wrong_answer():
    g = Game()
    g.start()
    g.round_trip({"p0": True})
    assert g.player("p0").streak == 1
    g.round_trip({"p0": False})
    assert g.player("p0").streak == 0
    g.round_trip({"p0": True})
    assert g.player("p0").streak == 1


def test_streak_resets_on_timeout():
    g = Game()
    g.start()
    g.round_trip({"p0": True})
    g.round_trip({})
    assert g.player("p0").streak == 0


def test_token_earned_every_streak_for_token_correct_answers():
    g = Game()
    g.start()
    tokens, earned = [], []
    for _ in range(4):
        rev = g.play_question({"p0": True})
        tokens.append(rev.outcomes["p0"].tokens)
        earned.append(rev.outcomes["p0"].token_earned)
        g.past_reveal()
        if g.phase is Phase.ATTACK:
            g.tick()
    assert tokens == [0, 1, 1, 2]
    assert earned == [False, True, False, True]
    assert g.player("p0").streak == 4


def test_streak_for_token_is_configurable():
    cfg = GameConfig.from_overrides(CFG.summary(), streak_for_token=3)
    g = Game(config=cfg)
    g.start()
    got = []
    for _ in range(6):
        rev = g.play_question({"p0": True})
        got.append(rev.outcomes["p0"].tokens)
        g.past_reveal()
        if g.phase is Phase.ATTACK:
            g.tick()
    assert got == [0, 0, 1, 1, 1, 2]


def test_tokens_are_capped_at_max_tokens():
    g = Game()
    g.start()
    for _ in range(6):  # streak 6 would be a 3rd token
        rev = g.play_question({"p0": True})
        g.past_reveal()
        if g.phase is Phase.ATTACK:
            g.tick()
    assert g.player("p0").tokens == CFG.max_tokens == 2
    assert rev.outcomes["p0"].token_earned is False
    assert g.player("p0").streak == 6


def test_a_broken_streak_restarts_the_count_toward_a_token():
    g = Game()
    g.start()
    g.round_trip({"p0": True})
    g.round_trip({"p0": False})
    g.round_trip({"p0": True})
    assert g.player("p0").tokens == 0
    g.round_trip({"p0": True})
    assert g.player("p0").tokens == 1


def test_absent_player_is_unchanged_and_keeps_the_streak():
    g = Game(config=LONG_REJOIN)
    g.start()
    g.round_trip({"p0": True})
    g.send(Disconnect("p0"))
    rev = g.play_question({"p1": True})
    assert rev.outcomes["p0"].outcome == "absent"
    assert rev.outcomes["p0"].points == 0
    assert (g.player("p0").xp, g.player("p0").streak) == (13, 1)
    g.past_reveal()
    g.send(Reconnect("p0"))
    g.round_trip({"p0": True})
    assert g.player("p0").streak == 2
    assert g.player("p0").tokens == 1  # the absence did not break the streak
    serves = [s for s in g.state.serves if s.player_id == "p0"]
    assert [s.outcome for s in serves] == ["correct", "absent", "correct"]
    assert serves[1].response_ms is None


def test_answer_then_disconnect_still_scores():
    g = Game(config=LONG_REJOIN)
    g.start()
    g.pick()
    g.answer("p0")
    g.send(Disconnect("p0"))
    g.tick()
    assert g.player("p0").xp == 13


def test_dropped_players_are_not_served_or_scored():
    g = Game()
    g.start()
    g.send(Disconnect("p2"))
    g.tick(at=g.now + CFG.rejoin_seconds * 1000)
    assert g.player("p2").dropped
    rev = g.play_question({"p0": True, "p1": True})
    assert "p2" not in rev.outcomes
    assert not [s for s in g.state.serves if s.player_id == "p2"]


# =====================================================================
# Answers: acceptance (§5), validation, early end
# =====================================================================


def test_answer_at_deadline_plus_grace_counts_and_one_ms_later_does_not():
    g = Game()
    g.start()
    g.pick()
    end = g.state.phase_end_ms
    ack = one(g.send(Answer("p0", g.state.question.id, g.correct()), at=end), AnswerAck)
    assert ack.accepted is True and ack.reason is None
    ack = one(g.send(Answer("p1", g.state.question.id, g.correct()), at=end + 1), AnswerAck)
    assert ack.accepted is False and ack.reason == "too_late"
    assert "p1" not in g.state.answers
    g.tick()
    rev = one(g.log, Revealed)
    assert rev.outcomes["p0"].outcome == "correct"
    assert rev.outcomes["p1"].outcome == "timeout"


def test_grace_is_configurable_and_zero_means_the_deadline():
    cfg = GameConfig.from_overrides(CFG.summary(), grace_ms=0)
    g = Game(config=cfg)
    g.start()
    g.pick()
    assert g.state.phase_end_ms == g.state.deadline_ms
    ack = one(g.send(Answer("p0", g.state.question.id, 0), at=g.state.deadline_ms + 1), AnswerAck)
    assert ack.accepted is False


def test_answer_validation():
    g = Game()
    g.start()
    assert errors(g.send(Answer("p0", "geo-0", 0))) == ["wrong_phase"]
    g.pick("geo")
    assert errors(g.send(Answer("p0", "geo-1", 0))) == ["wrong_question"]
    assert errors(g.send(Answer("p0", "geo-0", 4))) == ["bad_option"]
    assert errors(g.send(Answer("p0", "geo-0", -1))) == ["bad_option"]
    assert errors(g.send(Answer("zz", "geo-0", 0))) == ["unknown_player"]
    g.answer("p0")
    assert errors(g.send(Answer("p0", "geo-0", 1))) == ["already_answered"]
    assert g.state.answers["p0"].option == g.correct()


def test_question_ends_early_when_every_present_player_answered():
    g = Game(players=3)
    g.start()
    g.send(Disconnect("p2"))
    g.pick()
    g.answer("p0")
    assert g.phase is Phase.QUESTION
    msgs = g.answer("p1")
    assert g.phase is Phase.REVEAL
    assert one(msgs, Revealed).outcomes["p2"].outcome == "absent"


def test_last_unanswered_player_leaving_ends_the_question_early():
    g = Game(players=2)
    g.start()
    g.pick()
    g.answer("p0")
    g.send(Disconnect("p1"))
    assert g.phase is Phase.REVEAL


def test_response_time_subtracts_half_the_rtt_and_floors_at_zero():
    g = Game()
    g.start()
    g.pick()
    g.answer("p0", after_ms=1000, rtt_ms=300)
    g.answer("p1", after_ms=100, rtt_ms=1000)
    g.tick()
    serves = {s.player_id: s for s in g.state.serves}
    assert serves["p0"].response_ms == 850
    assert serves["p1"].response_ms == 0
    assert g.player("p0").correct_response_ms == [850]


def test_serves_are_recorded_for_every_active_player_each_round():
    g = Game(players=3)
    g.start()
    g.round_trip({"p0": True, "p1": False})
    g.round_trip({"p0": True})
    assert len(g.state.serves) == 6
    s = g.state.serves[0]
    assert s.question_id.endswith("-0")
    assert (s.player_id, s.outcome, s.round, s.kind) == ("p0", "correct", 1, "question")
    assert {x.outcome for x in g.state.serves} == {"correct", "incorrect", "timeout"}


# =====================================================================
# §2.4 attacks
# =====================================================================


def armed_game(players: int = 3, config: GameConfig = CFG, **kw) -> Game:
    """p0 holds a token and the game sits in ATTACK."""
    g = Game(players=players, config=config, **kw)
    g.start()
    g.earn_token("p0")
    g.play_question({"p0": True})
    g.past_reveal()
    assert g.phase is Phase.ATTACK
    return g


def test_attack_phase_is_skipped_when_nobody_present_holds_a_token():
    g = Game()
    g.start()
    g.play_question({"p0": True})
    msgs = g.tick()
    assert phases(msgs) == [Phase.PICK]
    assert g.state.round == 2


def test_attack_phase_is_skipped_when_the_only_holder_is_absent():
    g = Game(config=LONG_REJOIN)
    g.start()
    g.earn_token("p0")
    g.play_question({"p0": True})
    g.send(Disconnect("p0"))
    g.past_reveal()
    assert g.phase is Phase.PICK


def test_attack_phase_opens_for_a_holder_at_zero_xp():
    """XP is not considered: skipping the window would tell the room the
    holder is broke."""
    g = Game()
    g.start()
    g.earn_token("p0")
    g.play_question({"p0": True})
    g.player("p0").xp = 0
    g.past_reveal()
    assert g.phase is Phase.ATTACK


def test_attack_costs_the_token_and_attack_cost_xp_immediately():
    g = armed_game()
    xp, tokens = g.player("p0").xp, g.player("p0").tokens
    msgs = g.send(Attack("p0", "p1"))
    assert one(msgs, AttackDeclared) == AttackDeclared("p0", "p1")
    assert g.player("p0").tokens == tokens - 1
    assert g.player("p0").xp == xp - CFG.attack_cost
    assert g.state.attacks[0].attacker_id == "p0"


def test_attacker_pays_even_when_the_block_succeeds():
    g = armed_game()
    xp = g.player("p0").xp
    g.send(Attack("p0", "p1"))
    g.block_answer("p1", right=True)
    assert g.player("p0").xp == xp - 1
    assert g.player("p0").tokens == 0


def test_attack_needs_a_token():
    g = armed_game()
    assert errors(g.send(Attack("p1", "p0"))) == ["no_token"]


def test_player_at_zero_xp_cannot_attack_and_the_window_stays_open():
    g = armed_game()
    g.player("p0").xp = 0  # arrange directly: the rule is about the number
    msgs = g.send(Attack("p0", "p1"))
    assert errors(msgs) == ["no_xp"]
    assert [m for m in msgs if not isinstance(m, Error)] == []  # private refusal
    assert g.player("p0").tokens == 1
    assert g.phase is Phase.ATTACK
    g.tick()
    assert g.phase is Phase.PICK  # the deadline, not the refusal, closes it


def test_pass_ends_the_window_early_and_keeps_the_token():
    g = armed_game()
    msgs = g.send(Pass("p0"))
    assert msgs[0] == Passed("p0")
    assert errors(msgs) == []
    assert g.player("p0").tokens == 1
    assert g.phase is Phase.PICK  # nobody attacked → no BLOCK


def test_pass_with_other_holders_waits_for_them():
    g = Game(players=3)
    g.start()
    g.earn_token("p0", others=("p1",))
    g.play_question({"p0": True, "p1": True})
    g.past_reveal()
    g.send(Pass("p0"))
    assert g.phase is Phase.ATTACK
    g.send(Attack("p1", "p2"))
    assert g.phase is Phase.BLOCK
    assert g.state.blocks["p2"].attacker_ids == ["p1"]


def test_pass_validation():
    g = Game()
    g.start()
    assert errors(g.send(Pass("p0"))) == ["wrong_phase"]
    g = armed_game(players=4)
    g.player("p3").tokens = 1  # keeps the window open
    assert errors(g.send(Pass("p1"))) == ["no_token"]
    assert errors(g.send(Pass("ghost"))) == ["unknown_player"]
    g.send(Attack("p0", "p1"))
    assert errors(g.send(Pass("p0"))) == ["already_attacked"]  # attacked, then pass
    g.send(Pass("p3"))
    assert g.phase is Phase.BLOCK  # p0's attack still resolves
    g = armed_game()
    assert errors(g.send(Pass("p0"), at=g.state.phase_end_ms + 1)) == ["too_late"]


def test_attack_after_pass_is_refused():
    g = armed_game(players=4)
    g.player("p3").tokens = 1
    g.send(Pass("p0"))
    assert errors(g.send(Attack("p0", "p1"))) == ["already_attacked"]
    assert g.player("p0").tokens == 1


def test_attack_cost_is_floored_at_zero():
    cfg = GameConfig.from_overrides(CFG.summary(), attack_cost=5)
    g = armed_game(config=cfg)
    g.player("p0").xp = 2
    g.send(Attack("p0", "p1"))
    assert g.player("p0").xp == 0


def test_attack_target_validation():
    g = armed_game()
    assert errors(g.send(Attack("p0", "p0"))) == ["self_target"]
    assert errors(g.send(Attack("p0", "ghost"))) == ["unknown_target"]
    assert errors(g.send(Attack("ghost", "p0"))) == ["unknown_player"]
    assert g.player("p0").tokens == 1
    assert g.state.attacks == []


def test_dropped_player_cannot_be_targeted():
    g = Game(players=4)
    g.start()
    g.send(Disconnect("p3"))
    g.tick(at=g.now + CFG.rejoin_seconds * 1000)
    g.earn_token("p0")
    g.play_question({"p0": True})
    g.past_reveal()
    assert errors(g.send(Attack("p0", "p3"))) == ["unknown_target"]


def test_attack_outside_the_window_is_refused():
    g = Game()
    g.start()
    assert errors(g.send(Attack("p0", "p1"))) == ["wrong_phase"]
    g = armed_game()
    assert errors(g.send(Attack("p0", "p1"), at=g.state.phase_end_ms + 1)) == ["too_late"]
    assert g.player("p0").tokens == 1


def test_one_attack_per_window():
    g = armed_game(players=4)
    g.player("p0").tokens = 2
    g.player("p3").tokens = 1  # keeps the window open after p0's attack
    g.send(Attack("p0", "p1"))
    assert g.phase is Phase.ATTACK
    assert errors(g.send(Attack("p0", "p2"))) == ["already_attacked"]
    assert g.player("p0").tokens == 1


def test_incoming_attack_cap_refuses_the_third_attacker_who_keeps_token_and_xp():
    g = Game(players=5)
    g.start()
    g.earn_token("p0", others=("p1", "p2"))
    g.play_question({"p0": True, "p1": True, "p2": True})
    g.past_reveal()
    assert g.phase is Phase.ATTACK
    assert all(g.player(p).tokens == 1 for p in ("p0", "p1", "p2"))
    g.send(Attack("p0", "p4"))
    g.send(Attack("p1", "p4"))
    xp = g.player("p2").xp
    msgs = g.send(Attack("p2", "p4"))
    assert errors(msgs) == ["target_full"]
    assert g.player("p2").tokens == 1 and g.player("p2").xp == xp
    assert g.state.incoming_attacks("p4") == 2
    # the refused attacker may still pick someone else
    g.send(Attack("p2", "p3"))
    assert g.player("p2").tokens == 0
    assert g.phase is Phase.BLOCK
    assert set(g.state.blocks) == {"p4", "p3"}
    assert g.state.blocks["p4"].attacker_ids == ["p0", "p1"]


def test_incoming_cap_is_configurable():
    cfg = GameConfig.from_overrides(CFG.summary(), max_incoming_attacks=1)
    g = Game(players=4, config=cfg)
    g.start()
    g.earn_token("p0", others=("p1",))
    g.play_question({"p0": True, "p1": True})
    g.past_reveal()
    g.send(Attack("p0", "p3"))
    assert errors(g.send(Attack("p1", "p3"))) == ["target_full"]


def test_amendment_a_zero_xp_player_can_be_attacked_for_no_damage():
    g = armed_game()
    g.player("p1").xp = 0
    xp0 = g.player("p0").xp
    assert errors(g.send(Attack("p0", "p1"))) == []
    assert g.player("p0").xp == xp0 - 1  # attacker still pays
    g.tick()  # block timeout: the worst case for the target
    res = one(g.log, BlockResolved)
    assert (res.target_id, res.blocked, res.outcome, res.damage) == ("p1", False, "timeout", 0)
    assert g.player("p1").xp == 0


def test_attack_window_ends_early_once_every_eligible_attacker_acted():
    g = armed_game(players=3)
    assert g.state.awaiting_attack("p0") and not g.state.awaiting_attack("p1")
    g.send(Attack("p0", "p1"))
    assert g.phase is Phase.BLOCK


def test_attack_window_waits_for_a_second_eligible_attacker():
    g = Game(players=3)
    g.start()
    g.earn_token("p0", others=("p1",))
    g.play_question({"p0": True, "p1": True})
    g.past_reveal()
    g.send(Attack("p0", "p2"))
    assert g.phase is Phase.ATTACK
    g.send(Attack("p1", "p2"))
    assert g.phase is Phase.BLOCK


def test_attack_window_closes_early_when_the_last_eligible_attacker_leaves():
    g = armed_game()
    g.send(Disconnect("p0"))
    assert g.phase is Phase.PICK  # no attacks were declared → straight on


def test_attack_window_with_no_attacks_goes_to_the_next_round():
    g = armed_game()
    msgs = g.tick()
    assert phases(msgs) == [Phase.PICK]
    assert g.state.round == 4
    assert g.player("p0").tokens == 1  # unused token is kept


# =====================================================================
# §2.4 block
# =====================================================================


def test_block_questions_come_from_the_reserve_only_for_targets():
    g = armed_game(players=4)
    msgs = g.send(Attack("p0", "p2"))
    shown = one(msgs, BlockQuestionShown)
    assert (shown.target_id, shown.question_id, shown.attacker_ids) == ("p2", "blk-0", ("p0",))
    assert shown.deadline_ms == g.state.deadline_ms
    assert [q.id for q in g.state.block_reserve] == [f"blk-{i}" for i in range(1, 6)]
    assert set(g.state.blocks) == {"p2"}
    assert g.state.blocks["p2"].question.difficulty in (2, 3)


def test_correct_block_stops_every_incoming_attack():
    g = Game(players=4)
    g.start()
    g.earn_token("p0", others=("p1",))
    g.play_question({"p0": True, "p1": True})
    g.past_reveal()
    g.send(Attack("p0", "p3"))
    g.send(Attack("p1", "p3"))
    xp = g.player("p3").xp
    msgs = g.block_answer("p3", right=True)
    res = one(msgs, BlockResolved)
    assert (res.blocked, res.outcome, res.damage, res.attacker_ids) == (True, "correct", 0, ("p0", "p1"))
    assert g.player("p3").xp == xp
    assert res.delta == g.player("p3").delta
    assert g.phase is Phase.PICK


def test_failed_block_costs_attack_damage_per_incoming_attack():
    g = Game(players=4)
    g.start()
    g.earn_token("p0", others=("p1",))
    g.play_question({"p0": True, "p1": True})
    g.past_reveal()
    g.send(Attack("p0", "p3"))
    g.send(Attack("p1", "p3"))
    g.player("p3").xp = 10
    res = one(g.block_answer("p3", right=False), BlockResolved)
    assert (res.blocked, res.outcome, res.damage) == (False, "incorrect", 6)
    assert g.player("p3").xp == 4


def test_block_timeout_costs_damage():
    g = armed_game()
    g.send(Attack("p0", "p1"))
    xp = g.player("p1").xp
    res = one(g.tick(), BlockResolved)
    assert (res.blocked, res.outcome, res.damage) == (False, "timeout", 3)
    assert g.player("p1").xp == xp - 3
    assert g.phase is Phase.PICK


def test_block_damage_is_floored_at_zero():
    g = armed_game()
    g.send(Attack("p0", "p1"))
    g.player("p1").xp = 2
    res = one(g.block_answer("p1", right=False), BlockResolved)
    assert res.damage == 2
    assert g.player("p1").xp == 0


def test_block_answers_never_touch_streaks_or_tokens():
    g = Game(players=3)
    g.start()
    g.earn_token("p0", others=("p1",))
    g.play_question({"p0": True, "p1": True})
    g.past_reveal()
    # p1 has streak 3 and a token; p0 attacks p1
    assert (g.player("p1").streak, g.player("p1").tokens) == (3, 1)
    g.send(Attack("p0", "p1"))
    if g.phase is Phase.ATTACK:
        g.tick()  # p1 also holds a token, so the window waits for them
    g.block_answer("p1", right=False)
    assert (g.player("p1").streak, g.player("p1").tokens) == (3, 1)
    g.pick()
    g.answer("p1")
    g.tick()
    assert g.player("p1").streak == 4  # the failed block did not break it
    assert g.player("p1").tokens == 2


def test_correct_block_does_not_extend_a_streak_or_earn_a_token():
    g = armed_game()
    g.player("p1").streak = 1
    g.send(Attack("p0", "p1"))
    g.block_answer("p1", right=True)
    assert (g.player("p1").streak, g.player("p1").tokens) == (1, 0)
    assert g.player("p1").correct_response_ms == []


def test_block_answer_validation():
    g = armed_game()
    g.send(Attack("p0", "p1"))
    qid = g.state.blocks["p1"].question.id
    assert errors(g.send(Answer("p0", qid, 0))) == ["not_attacked"]
    assert errors(g.send(Answer("p2", qid, 0))) == ["not_attacked"]
    assert errors(g.send(Answer("p1", "geo-0", 0))) == ["wrong_question"]
    assert errors(g.send(Answer("p1", qid, 7))) == ["bad_option"]
    ack = one(g.send(Answer("p1", qid, 0), at=g.state.phase_end_ms + 1), AnswerAck)
    assert ack.accepted is False and ack.reason == "too_late"
    g.send(Answer("p1", qid, 0), at=g.state.phase_end_ms)  # within grace
    assert g.phase is Phase.PICK


def test_block_answer_within_grace_is_accepted():
    g = armed_game()
    g.send(Attack("p0", "p1"))
    ack = one(g.send(Answer("p1", "blk-0", 0), at=g.state.phase_end_ms), AnswerAck)
    assert ack.accepted


def test_block_phase_waits_for_every_target():
    g = Game(players=4)
    g.start()
    g.earn_token("p0", others=("p1",))
    g.play_question({"p0": True, "p1": True})
    g.past_reveal()
    g.send(Attack("p0", "p2"))
    g.send(Attack("p1", "p3"))
    g.block_answer("p2")
    assert g.phase is Phase.BLOCK
    g.block_answer("p3")
    assert g.phase is Phase.PICK


def test_absent_target_times_out_and_takes_damage():
    """Absence only protects round answers (§2.3); a block is still due."""
    g = armed_game()
    g.send(Attack("p0", "p1"))
    g.send(Disconnect("p1"))
    xp = g.player("p1").xp
    res = one(g.tick(), BlockResolved)
    assert (res.blocked, res.outcome, res.damage) == (False, "timeout", 3)
    assert g.player("p1").xp == xp - 3
    serve = [s for s in g.state.serves if s.kind == "block"][0]
    assert (serve.player_id, serve.outcome, serve.response_ms) == ("p1", "timeout", None)


def test_empty_reserve_draws_a_hard_unused_pool_question():
    g = armed_game(reserve=[])
    msgs = g.send(Attack("p0", "p1"))
    shown = one(msgs, BlockQuestionShown)
    block = g.state.blocks["p1"]
    assert shown.question_id == block.question.id
    assert block.question.difficulty >= 2
    assert block.question.category_id in CATS
    assert all(block.question not in pool for pool in g.state.pools.values())
    assert sum(len(pool) for pool in g.state.pools.values()) == 3 * 8 - 3 - 1
    xp = g.player("p1").xp
    g.block_answer("p1", right=False)
    assert g.player("p1").xp == xp - 3  # a pool question blocks like any other


def test_empty_reserve_falls_back_to_any_unused_question_when_none_are_hard():
    easy = {c: [Question(f"{c}-{i}", c, 1, i % 4) for i in range(4)] for c in CATS}
    g = armed_game(pools=easy, reserve=[])
    g.send(Attack("p0", "p1"))
    assert g.state.blocks["p1"].question.difficulty == 1


def test_block_fallback_draw_is_deterministic_for_a_seed():
    def draw(seed):
        g = armed_game(reserve=[], rng=ScriptedRng(seed=seed))
        g.send(Attack("p0", "p1"))
        return g.state.blocks["p1"].question.id

    assert draw(3) == draw(3)


def test_attacks_count_as_blocked_only_when_every_pool_is_empty():
    pools = {"geo": [Question(f"geo-{i}", "geo", 1, i % 4) for i in range(3)]}
    g = armed_game(pools=pools, reserve=[])  # round 3 used the last question
    assert g.state.remaining_categories() == []
    msgs = g.send(Attack("p0", "p1"))
    assert not [m for m in msgs if isinstance(m, BlockQuestionShown)]
    res = one(msgs, BlockResolved)
    assert (res.blocked, res.damage) == (True, 0)
    assert g.phase is Phase.END  # nothing left to play either
    assert not [s for s in g.state.serves if s.kind == "block"]


def test_block_serve_is_recorded():
    g = armed_game()
    g.send(Attack("p0", "p1"))
    g.block_answer("p1", right=False, after_ms=700)
    serve = [s for s in g.state.serves if s.kind == "block"][0]
    assert (serve.question_id, serve.player_id, serve.outcome, serve.response_ms) == (
        "blk-0", "p1", "incorrect", 700,
    )
    assert serve.round == g.state.round - 1


def test_attacks_then_blocks_then_next_round_sequence():
    g = armed_game()
    g.send(Attack("p0", "p1"))
    g.block_answer("p1")
    since_attack = phases(g.log[g.log.index(one(g.log, AttackDeclared)) :])
    assert since_attack == [Phase.BLOCK, Phase.PICK]


# =====================================================================
# §2.8 presence
# =====================================================================


def test_disconnect_and_reconnect_are_announced_once():
    g = Game()
    g.start()
    assert one(g.send(Disconnect("p1")), PresenceChanged).status == "absent"
    assert g.send(Disconnect("p1")) == []  # idempotent
    assert not g.player("p1").present
    assert g.player("p1").absent_since_ms == g.now
    assert one(g.send(Reconnect("p1")), PresenceChanged).status == "returned"
    assert g.send(Reconnect("p1")) == []
    assert g.player("p1").present
    assert g.player("p1").absent_since_ms is None


def test_player_is_dropped_after_rejoin_seconds():
    g = Game()
    g.start()
    g.send(Disconnect("p1"))
    limit = g.now + CFG.rejoin_seconds * 1000
    assert g.tick(at=limit - 1) == []
    assert one(g.tick(at=limit), PresenceChanged) == PresenceChanged("p1", "dropped")
    assert g.player("p1").dropped
    assert errors(g.send(Reconnect("p1"))) == ["dropped"]
    assert "p1" in g.state.players  # remains in results (§2.8)


def test_reconnect_just_in_time_keeps_seat_and_xp():
    g = Game()
    g.start()
    g.round_trip({"p1": True})
    g.send(Disconnect("p1"))
    g.send(Reconnect("p1"), at=g.now + CFG.rejoin_seconds * 1000 - 1)
    g.tick(at=g.now + 10**6)
    assert not g.player("p1").dropped
    assert g.player("p1").xp == 13


def test_picker_absent_at_round_start_passes_to_next_present_player():
    g = Game(players=3, config=LONG_REJOIN)
    g.start()
    g.send(Disconnect("p1"))
    g.round_trip({})
    assert g.state.picker_id == "p2"
    g.send(Reconnect("p1"))
    g.round_trip({})
    assert g.state.picker_id == "p0"
    g.round_trip({})
    assert g.state.picker_id == "p1"


def test_picker_leaving_during_pick_passes_the_pick_immediately():
    g = Game(players=3)
    g.start()
    deadline = g.state.deadline_ms
    msgs = g.send(Disconnect("p0"))
    assert g.state.picker_id == "p1"
    assert one(msgs, BoardShown).picker_id == "p1"
    assert g.state.deadline_ms == deadline  # the clock keeps running
    assert errors(g.send(Pick("p0", g.state.board[0]))) == ["not_your_pick"]
    g.pick()
    assert g.phase is Phase.QUESTION


def test_host_passes_to_the_longest_connected_present_player():
    g = Game(join=False)
    g.send(Join("h", "Host"), at=T0)
    g.send(Join("a", "Ann"), at=T0 + 1000)
    g.send(Join("b", "Bob"), at=T0 + 2000)
    g.send(Disconnect("a"), at=T0 + 3000)
    g.send(Reconnect("a"), at=T0 + 4000)  # reconnecting resets the clock
    msgs = g.send(Disconnect("h"), at=T0 + 5000)
    assert one(msgs, HostChanged).host_id == "b"
    assert g.state.host_id == "b"
    g.send(Reconnect("h"))
    assert g.state.host_id == "b"  # no automatic hand-back
    assert errors(g.start(by="h")) == ["not_host"]
    g.start(by="b")
    assert g.phase is Phase.PICK


def test_host_stays_when_nobody_else_is_present():
    g = Game(players=2)
    g.send(Disconnect("p1"))
    assert not [m for m in g.send(Disconnect("p0")) if isinstance(m, HostChanged)]
    assert g.state.host_id == "p0"


def test_session_is_abandoned_after_abandon_seconds_with_fewer_than_two_present():
    g = Game(players=3)
    g.start()
    g.send(Disconnect("p1"))
    g.send(Disconnect("p2"), at=T0 + 1000)
    limit = T0 + 1000 + CFG.abandon_seconds * 1000
    g.tick(at=limit - 1)
    assert g.phase is not Phase.END
    g.tick(at=limit)
    assert g.phase is Phase.END
    ended = one(g.log, Ended)
    assert ended.reason == "abandoned"
    assert g.state.end_reason == "abandoned"
    assert len(ended.results) == 3


def test_abandon_timer_clears_when_someone_returns():
    g = Game(players=2)
    g.start()
    g.send(Disconnect("p1"))
    assert g.state.low_presence_since_ms == T0
    g.send(Reconnect("p1"), at=T0 + 30_000)
    assert g.state.low_presence_since_ms is None
    g.tick(at=T0 + 10**6)
    assert g.phase is not Phase.END


def test_abandon_rule_does_not_apply_in_the_lobby():
    g = Game(players=2)
    g.send(Disconnect("p1"))
    g.tick(at=T0 + 10**7)
    assert g.phase is Phase.LOBBY
    assert g.state.low_presence_since_ms is None


def test_dropping_the_second_to_last_player_starts_the_abandon_clock_at_disconnect():
    g = Game(players=2)
    g.start()
    g.send(Disconnect("p1"), at=T0 + 500)
    assert g.state.low_presence_since_ms == T0 + 500


# =====================================================================
# §2.6 / §2.7 end: reveal and winner tiebreaks
# =====================================================================


def finish(g: Game) -> Ended:
    while g.phase is not Phase.END:
        g.round_trip({})
    return one(g.log, Ended)


def test_end_reveals_starting_final_and_delta_for_everyone():
    rng = ScriptedRng(choices=[30, 10, 18])
    g = Game(players=3, config=GameConfig(min_players=2, question_count=2), rng=rng)
    g.start()
    g.round_trip({"p0": False, "p1": True, "p2": True})
    g.round_trip({"p1": True})
    ended = one(g.log, Ended)
    by_id = {r.player_id: r for r in ended.results}
    assert (by_id["p0"].starting_xp, by_id["p0"].final_xp, by_id["p0"].delta) == (30, 28, -2)
    assert (by_id["p1"].starting_xp, by_id["p1"].final_xp, by_id["p1"].delta) == (10, 16, 6)
    assert (by_id["p2"].starting_xp, by_id["p2"].final_xp, by_id["p2"].delta) == (18, 20, 2)
    assert by_id["p0"].display_name == "Player 0"
    assert [r.player_id for r in ended.results] == ["p0", "p2", "p1"]  # ranked
    assert ended.winner_ids == ("p0",) and ended.tiebreak is None
    assert g.state.results == list(ended.results)


def test_highest_final_total_wins_even_with_a_lower_delta():
    rng = ScriptedRng(choices=[30, 10])
    g = Game(players=2, config=GameConfig(min_players=2, question_count=1), rng=rng)
    g.start()
    g.round_trip({"p0": False, "p1": True})
    ended = one(g.log, Ended)
    assert ended.winner_ids == ("p0",)  # 29 beats 13


def test_tie_on_total_goes_to_the_higher_delta():
    rng = ScriptedRng(choices=[18, 10])  # p0 starts 18, p1 starts 10
    g = Game(players=2, config=GameConfig(min_players=2, question_count=4), rng=rng)
    g.start()
    # p1 needs +8 over p0: p0 wrong (-1) & p1 correct (+3) = 4 per round → 2 rounds
    g.round_trip({"p0": False, "p1": True})
    g.round_trip({"p0": False, "p1": True})
    ended = finish(g)
    assert g.player("p0").xp == g.player("p1").xp
    assert ended.winner_ids == ("p1",)
    assert ended.tiebreak == "delta"


def test_tie_on_total_and_delta_goes_to_faster_correct_answers():
    g = Game(players=2, config=GameConfig.from_overrides(CFG.summary(), question_count=2))
    g.start()
    g.pick()
    g.answer("p0", after_ms=2000)
    g.answer("p1", after_ms=500)
    g.tick()
    g.pick()
    g.answer("p0", after_ms=1000)
    g.answer("p1", after_ms=3000)
    g.past_reveal()
    if g.phase is Phase.ATTACK:  # both earned a token on the 2nd correct answer
        g.tick()
    ended = one(g.log, Ended)
    assert g.player("p0").xp == g.player("p1").xp
    assert g.player("p0").mean_correct_ms == 1500 < g.player("p1").mean_correct_ms == 1750
    assert ended.winner_ids == ("p0",)
    assert ended.tiebreak == "response_time"


def test_response_time_tiebreak_ignores_block_answers():
    """A blazing block answer must not count as a fast correct answer."""
    g = Game(players=3)
    g.start()
    g.earn_token("p0", others=("p1", "p2"))
    g.play_question({"p0": True, "p1": True, "p2": True})
    g.past_reveal()
    g.send(Attack("p0", "p1"))
    g.send(Attack("p2", "p1"))
    if g.phase is Phase.ATTACK:
        g.tick()
    g.block_answer("p1", right=True, after_ms=1)
    assert 1 not in g.player("p1").correct_response_ms


def test_player_with_no_correct_answers_loses_the_response_time_tiebreak():
    cfg = GameConfig.from_overrides(CFG.summary(), starting_xp_choices=(0,), question_count=1)
    g = Game(players=2, config=cfg)
    g.start()
    g.pick()
    g.answer("p0", right=True)
    g.tick()
    g.player("p1").xp = 3  # arrange: same total, same delta, never answered
    g.player("p1").starting_xp = 0
    g.tick()  # reveal → end
    ended = one(g.log, Ended)
    assert ended.winner_ids == ("p0",)
    assert ended.tiebreak == "response_time"


def test_complete_tie_is_a_shared_win():
    g = Game(players=3, config=GameConfig.from_overrides(CFG.summary(), question_count=1))
    g.start()
    g.pick()
    g.answer("p0", after_ms=800)
    g.answer("p1", after_ms=800)
    g.answer("p2", after_ms=900)
    g.tick()
    ended = one(g.log, Ended)
    assert ended.winner_ids == ("p0", "p1")
    assert ended.tiebreak == "shared"


def test_shared_win_among_players_who_never_answered():
    g = Game(players=2, config=GameConfig.from_overrides(CFG.summary(), question_count=1))
    g.start()
    g.round_trip({})
    ended = one(g.log, Ended)
    assert ended.winner_ids == ("p0", "p1")
    assert ended.tiebreak == "shared"
    assert all(r.mean_correct_ms is None for r in ended.results)


def test_dropped_players_remain_in_the_final_results():
    g = Game(players=3, config=GameConfig.from_overrides(CFG.summary(), question_count=2))
    g.start()
    g.round_trip({"p2": True})
    g.send(Disconnect("p2"))
    g.tick(at=g.now + CFG.rejoin_seconds * 1000)
    g.round_trip({"p0": True})
    ended = one(g.log, Ended)
    by_id = {r.player_id: r for r in ended.results}
    assert by_id["p2"].final_xp == 13 and by_id["p2"].delta == 3
    assert set(by_id) == {"p0", "p1", "p2"}


def test_no_input_is_accepted_after_end():
    g = Game(players=2, config=GameConfig.from_overrides(CFG.summary(), question_count=1))
    g.start()
    g.round_trip({})
    assert errors(g.send(Pick("p0", "geo"))) == ["wrong_phase"]
    assert errors(g.send(Answer("p0", "geo-0", 0))) == ["wrong_phase"]
    assert errors(g.send(Attack("p0", "p1"))) == ["wrong_phase"]
    assert errors(g.send(Join("x", "X"))) == ["wrong_phase"]
    assert errors(g.send(Start("p0"))) == ["wrong_phase"]


# =====================================================================
# Invariants over many random games
# =====================================================================


@pytest.mark.parametrize("seed", range(25))
def test_random_games_keep_every_invariant(seed):
    rng = random.Random(seed)
    cfg = GameConfig(
        min_players=2,
        question_count=rng.randint(3, 10),
        max_incoming_attacks=rng.randint(1, 3),
        max_tokens=rng.randint(1, 3),
        starting_xp_choices=(rng.choice([0, 5]), 10, 18, 30),
    )
    n = rng.randint(2, 8)
    g = Game(players=n, config=cfg, pools=make_pools(per_cat=5), reserve=make_reserve(4), rng=random.Random(seed))
    g.start()
    rounds_seen = 0
    for _ in range(2000):
        if g.phase is Phase.END:
            break
        s = g.state
        for p in s.players.values():
            assert p.xp >= 0
            assert 0 <= p.tokens <= cfg.max_tokens
            assert p.delta == p.xp - p.starting_xp
        for p in s.active_players():
            assert s.incoming_attacks(p.id) <= cfg.max_incoming_attacks
        assert s.round <= cfg.question_count
        if s.phase is Phase.PICK:
            rounds_seen += 1
            assert s.picker_id is not None and s.players[s.picker_id].active
            assert 1 <= len(s.board) <= cfg.board_size
            g.pick(rng.choice(s.board)) if rng.random() < 0.8 else g.tick()
        elif s.phase is Phase.QUESTION:
            for p in s.active_players():
                r = rng.random()
                if r < 0.7:
                    g.answer(p.id, rng.random() < 0.6, after_ms=rng.randint(0, 11_000))
            if g.phase is Phase.QUESTION:
                g.tick()
        elif s.phase is Phase.ATTACK:
            for p in list(s.players.values()):
                if rng.random() < 0.6:
                    target = rng.choice(list(s.players))
                    g.send(Attack(p.id, target))
            if g.phase is Phase.ATTACK:
                g.tick()
        elif s.phase is Phase.BLOCK:
            for pid in list(s.blocks):
                if rng.random() < 0.6:
                    g.block_answer(pid, rng.random() < 0.5, after_ms=rng.randint(0, 6000))
            if g.phase is Phase.BLOCK:
                g.tick()
        elif s.phase is Phase.REVEAL:
            if rng.random() < 0.1:
                victim = rng.choice(list(s.players))
                g.send(Disconnect(victim) if s.players[victim].present else Reconnect(victim))
            g.tick()
    assert g.phase is Phase.END
    ended = one(g.log, Ended)
    assert set(r.player_id for r in ended.results) == set(g.state.players)
    assert ended.winner_ids
    top = ended.results[0]
    assert all(r.final_xp <= top.final_xp for r in ended.results)
    assert all(g.state.players[w].xp == top.final_xp for w in ended.winner_ids)
    # every round question was served once per active player, no repeats
    round_serves = [s for s in g.state.serves if s.kind == "question"]
    assert len({(s.question_id, s.player_id) for s in round_serves}) == len(round_serves)
    assert rounds_seen == g.state.round


def test_message_types_are_frozen_facts():
    for kind in (Error, PhaseChanged, Revealed, AttackDeclared, BlockResolved, Ended):
        assert kind.__dataclass_params__.frozen
        assert fields(kind)
