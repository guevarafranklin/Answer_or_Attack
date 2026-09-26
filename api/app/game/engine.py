"""The pure rules engine (spec §1 rule 1, §2, §3).

    step(state, event, now_ms, rng) -> (state, [Message])

No I/O, no clock, no randomness of its own: server time arrives as `now_ms`
(epoch milliseconds, stamped by the runtime on receipt) and every random
choice — starting XP, the board, auto-pick — goes through the `rng` passed
in. Replaying the same events with the same times and the same seeded RNG
reproduces a game exactly (§6, session_events).

Messages are recipient-agnostic facts. Deciding who may see what — hiding
`correct_option` until reveal, totals until END, other players' tokens —
is the protocol layer's job (build step 4); the engine records everything
and leaks nothing by itself because it never talks to a client.

Decisions the spec leaves open, all deliberate and all tested:

* Every input phase (PICK, QUESTION, ATTACK, BLOCK) ends at
  `deadline_ms + grace_ms` (§5); REVEAL, which takes no input, ends at its
  deadline. `state.phase_end_ms` is when the runtime should send `Tick`.
* A phase ends early when nobody present can still act: every present
  player answered, the picker picked, every present token holder attacked
  or passed, every attacked player answered their block.
* ATTACK is skipped entirely when no present player holds a token. XP is
  not considered: a holder at 0 XP is refused privately with `no_xp` and
  can `Pass`, so the room never learns who is broke.
* "Absent = no change" protects round answers only (§2.3). An attacked
  player who is absent at the block deadline times out and takes damage.
* When the block reserve is empty, block questions are drawn from the
  unused questions of the category pools (difficulty ≥ 2 first, chosen by
  the rng); only with every pool empty do the attacks count as blocked.
* Dropped players (absent longer than `rejoin_seconds`) are served no
  more questions and cannot be attacked, but stay in the final results.
* Response time = received − shown − rtt/2, with the player's measured
  RTT arriving on the Answer event (§5). Tiebreak means use only correct
  answers to round questions, never blocks.
* "Can pay" for an attack means `xp > 0`, literally per §2.4; the cost is
  floored at 0.
* The floor itself is a secret: a delta that stops falling would say "this
  player is at 0", and with the deltas known that gives away the start.
  So every player carries two deltas — `nominal_delta`, the sum of every
  scoring change as nominally applied (points, attack cost, block damage,
  steal), and the real `delta` (floored xp − starting_xp). Messages before
  `Ended` only ever carry the nominal one; scoring and the tiebreak use
  the real one; `Ended` shows both.
"""
from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol

from app.game.config import GameConfig
from app.rules import OPTION_COUNT


class Rng(Protocol):
    """The slice of random.Random the engine uses."""

    def choice[T](self, seq: Sequence[T], /) -> T: ...
    def sample[T](self, population: Sequence[T], k: int, /) -> list[T]: ...


class Phase(StrEnum):
    LOBBY = "lobby"
    PICK = "pick"
    QUESTION = "question"
    REVEAL = "reveal"
    ATTACK = "attack"
    BLOCK = "block"
    END = "end"


# Matches question_serves.outcome (app.models._common.SERVE_OUTCOMES) so the
# END persistence can hand ServeRecords straight to record_serves.
Outcome = Literal["correct", "incorrect", "timeout", "absent"]
EndReason = Literal["finished", "abandoned"]
Tiebreak = Literal["delta", "response_time", "shared"] | None


# ---------- state ----------


@dataclass(frozen=True, slots=True)
class Question:
    """One drawn question, options already in shown order (the draw stores
    the per-session shuffle, §6). The engine needs no text."""

    id: str
    category_id: str
    difficulty: int
    correct_option: int

    def __deepcopy__(self, memo: dict) -> Question:
        return self  # immutable: sharing it between state copies is safe


@dataclass(slots=True)
class Player:
    id: str
    display_name: str
    connected_since_ms: int
    starting_xp: int = 0
    xp: int = 0  # real, floored at 0 — secret until the end
    nominal_delta: int = 0  # every change as applied, floor ignored — public
    streak: int = 0
    tokens: int = 0
    present: bool = True
    dropped: bool = False
    absent_since_ms: int | None = None
    # Response times of correct round answers, for the §2.7 tiebreak.
    correct_response_ms: list[int] = field(default_factory=list)

    @property
    def delta(self) -> int:
        """The real delta; never sent before the end."""
        return self.xp - self.starting_xp

    def score(self, points: int) -> None:
        """Apply a scoring change: the nominal delta takes it in full, the
        real XP takes what the floor allows."""
        self.nominal_delta += points
        self.xp = max(0, self.xp + points)

    @property
    def active(self) -> bool:
        """In the turn order: present and not dropped."""
        return self.present and not self.dropped

    @property
    def mean_correct_ms(self) -> float | None:
        if not self.correct_response_ms:
            return None
        return sum(self.correct_response_ms) / len(self.correct_response_ms)


@dataclass(slots=True)
class AnswerRecord:
    option: int
    received_ms: int
    response_ms: int


@dataclass(slots=True)
class PendingAttack:
    attacker_id: str
    target_id: str


@dataclass(slots=True)
class BlockChallenge:
    target_id: str
    attacker_ids: list[str]
    # None only when the reserve and every category pool are empty: the
    # target cannot be asked anything, so the attacks count as blocked.
    question: Question | None
    answer: AnswerRecord | None = None


@dataclass(slots=True)
class ServeRecord:
    """One question shown to one player; becomes a question_serves row."""

    question_id: str
    player_id: str
    outcome: Outcome
    response_ms: int | None
    round: int
    kind: Literal["question", "block"]


@dataclass(slots=True)
class PlayerResult:
    player_id: str
    display_name: str
    starting_xp: int
    final_xp: int
    delta: int  # real: final_xp - starting_xp
    nominal_delta: int  # what the room watched all game
    mean_correct_ms: float | None


@dataclass(slots=True)
class GameState:
    config: GameConfig
    # Ramped question pool per category id, plus the block reserve (§2.5 B).
    pools: dict[str, list[Question]]
    block_reserve: list[Question]
    phase: Phase = Phase.LOBBY
    round: int = 0
    players: dict[str, Player] = field(default_factory=dict)  # join order
    host_id: str | None = None
    picker_cursor: int = -1  # index into join order of the last picker
    picker_id: str | None = None
    board: list[str] = field(default_factory=list)
    question: Question | None = None
    question_sent_ms: int | None = None
    answers: dict[str, AnswerRecord] = field(default_factory=dict)
    attacks: list[PendingAttack] = field(default_factory=list)
    acted_this_window: set[str] = field(default_factory=set)  # attacked or passed
    blocks: dict[str, BlockChallenge] = field(default_factory=dict)
    deadline_ms: int | None = None  # what clients count down to
    phase_end_ms: int | None = None  # when the runtime should Tick
    low_presence_since_ms: int | None = None
    serves: list[ServeRecord] = field(default_factory=list)
    results: list[PlayerResult] | None = None
    end_reason: EndReason | None = None

    # ----- read helpers (also handy for the runtime and the simulator) -----

    def turn_order(self) -> list[Player]:
        return list(self.players.values())

    def active_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.active]

    def present_count(self) -> int:
        return len(self.active_players())

    def remaining_categories(self) -> list[str]:
        return [cid for cid, pool in self.pools.items() if pool]

    def incoming_attacks(self, target_id: str) -> int:
        return sum(1 for a in self.attacks if a.target_id == target_id)

    def awaiting_attack(self, player_id: str) -> bool:
        """Holds a token and has neither attacked nor passed this window:
        the ATTACK phase stays open for them (XP is deliberately not
        considered, see the module docstring)."""
        p = self.players.get(player_id)
        return (
            p is not None
            and p.active
            and p.tokens > 0
            and player_id not in self.acted_this_window
        )

    def anyone_awaiting_attack(self) -> bool:
        return any(self.awaiting_attack(p.id) for p in self.players.values())


# ---------- events (client intentions + runtime signals) ----------


@dataclass(frozen=True, slots=True)
class Join:
    player_id: str
    display_name: str
    as_host: bool = False  # the first joiner is host unless told otherwise


@dataclass(frozen=True, slots=True)
class Start:
    player_id: str


@dataclass(frozen=True, slots=True)
class Pick:
    player_id: str
    category_id: str


@dataclass(frozen=True, slots=True)
class Answer:
    player_id: str
    question_id: str
    option: int
    rtt_ms: int = 0  # the player's measured round trip, for response time


@dataclass(frozen=True, slots=True)
class Attack:
    player_id: str
    target_player_id: str


@dataclass(frozen=True, slots=True)
class Pass:
    """A token holder declines to attack this window."""

    player_id: str


@dataclass(frozen=True, slots=True)
class Disconnect:
    player_id: str


@dataclass(frozen=True, slots=True)
class Reconnect:
    player_id: str


@dataclass(frozen=True, slots=True)
class Tick:
    """A deadline may have passed: `phase_end_ms`, a rejoin deadline, or
    the abandon deadline. Harmless when sent early."""


Event = Join | Start | Pick | Answer | Attack | Pass | Disconnect | Reconnect | Tick


# ---------- outbound messages (facts; recipients decided in step 4) ----------


@dataclass(frozen=True, slots=True)
class Error:
    player_id: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class PlayerJoined:
    player_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class HostChanged:
    host_id: str


@dataclass(frozen=True, slots=True)
class PresenceChanged:
    player_id: str
    status: Literal["absent", "returned", "dropped"]


@dataclass(frozen=True, slots=True)
class PhaseChanged:
    phase: Phase
    round: int
    deadline_ms: int | None
    phase_end_ms: int | None


@dataclass(frozen=True, slots=True)
class BoardShown:
    picker_id: str
    category_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuestionShown:
    question_id: str
    category_id: str
    deadline_ms: int


@dataclass(frozen=True, slots=True)
class AnswerAck:
    player_id: str
    question_id: str
    accepted: bool
    reason: str | None = None  # "too_late" when refused


@dataclass(frozen=True, slots=True)
class PlayerOutcome:
    outcome: Outcome
    points: int  # nominal; the XP floor can absorb a negative one
    delta: int  # nominal (Player.nominal_delta)
    streak: int
    tokens: int
    token_earned: bool


@dataclass(frozen=True, slots=True)
class Revealed:
    question_id: str
    correct_option: int
    outcomes: dict[str, PlayerOutcome]  # by player id, active players only


@dataclass(frozen=True, slots=True)
class AttackDeclared:
    attacker_id: str
    target_id: str


@dataclass(frozen=True, slots=True)
class Passed:
    """Ack for the passer only: telling the room would reveal a token."""

    player_id: str


@dataclass(frozen=True, slots=True)
class BlockQuestionShown:
    target_id: str
    question_id: str
    attacker_ids: tuple[str, ...]
    deadline_ms: int


@dataclass(frozen=True, slots=True)
class BlockResolved:
    target_id: str
    attacker_ids: tuple[str, ...]
    blocked: bool
    outcome: Outcome
    damage: int  # nominal: attack_damage per attacker; the floor may absorb some
    delta: int  # the target's new nominal delta
    steal: int = 0  # XP each attacker gained (config.attack_steal on a failed block)


@dataclass(frozen=True, slots=True)
class Ended:
    reason: EndReason
    results: tuple[PlayerResult, ...]  # ranked, winners first
    winner_ids: tuple[str, ...]
    tiebreak: Tiebreak


Message = (
    Error
    | PlayerJoined
    | HostChanged
    | PresenceChanged
    | PhaseChanged
    | BoardShown
    | QuestionShown
    | AnswerAck
    | Revealed
    | AttackDeclared
    | Passed
    | BlockQuestionShown
    | BlockResolved
    | Ended
)


# ---------- entry points ----------


def new_game(
    config: GameConfig,
    pools: dict[str, Sequence[Question]],
    block_reserve: Sequence[Question],
) -> GameState:
    """A lobby with the drawn questions. `pools` are already ramped
    easy→hard per category; the engine serves them front to back."""
    return GameState(
        config=config,
        pools={cid: list(qs) for cid, qs in pools.items()},
        block_reserve=list(block_reserve),
    )


def step(
    state: GameState, event: Event, now_ms: int, rng: Rng, *, copy_state: bool = True
) -> tuple[GameState, list[Message]]:
    """Apply one event at server time `now_ms`. Returns the new state and
    the messages it produced; the input state is left untouched unless
    `copy_state=False` (the balance simulator's hot loop)."""
    s = copy.deepcopy(state) if copy_state else state
    out: list[Message] = []
    match event:
        case Join():
            _on_join(s, event, now_ms, out)
        case Start():
            _on_start(s, event, now_ms, rng, out)
        case Pick():
            _on_pick(s, event, now_ms, out)
        case Answer():
            _on_answer(s, event, now_ms, rng, out)
        case Attack():
            _on_attack(s, event, now_ms, rng, out)
        case Pass():
            _on_pass(s, event, now_ms, rng, out)
        case Disconnect():
            _on_disconnect(s, event, now_ms, rng, out)
        case Reconnect():
            _on_reconnect(s, event, now_ms, rng, out)
        case Tick():
            _on_tick(s, now_ms, rng, out)
        case _:
            raise TypeError(f"unknown event {event!r}")
    return s, out


# ---------- event handlers ----------


def _err(out: list[Message], player_id: str, code: str, message: str) -> None:
    out.append(Error(player_id, code, message))


def _player(s: GameState, player_id: str, out: list[Message]) -> Player | None:
    p = s.players.get(player_id)
    if p is None:
        _err(out, player_id, "unknown_player", "you are not in this session")
        return None
    if p.dropped:
        _err(out, player_id, "dropped", "you were dropped from this session")
        return None
    return p


def _on_join(s: GameState, ev: Join, now: int, out: list[Message]) -> None:
    if s.phase is not Phase.LOBBY:
        _err(out, ev.player_id, "wrong_phase", "the game has already started")
        return
    if ev.player_id in s.players:
        _err(out, ev.player_id, "already_joined", "already in this session")
        return
    if len(s.players) >= s.config.max_players:
        _err(out, ev.player_id, "session_full", "the session is full")
        return
    s.players[ev.player_id] = Player(ev.player_id, ev.display_name, connected_since_ms=now)
    out.append(PlayerJoined(ev.player_id, ev.display_name))
    if ev.as_host or s.host_id is None:
        s.host_id = ev.player_id
        out.append(HostChanged(ev.player_id))


def _on_start(s: GameState, ev: Start, now: int, rng: Rng, out: list[Message]) -> None:
    if _player(s, ev.player_id, out) is None:
        return
    if s.phase is not Phase.LOBBY:
        _err(out, ev.player_id, "wrong_phase", "the game has already started")
        return
    if ev.player_id != s.host_id:
        _err(out, ev.player_id, "not_host", "only the host can start")
        return
    # Players who gave up in the lobby are not part of the game at all.
    s.players = {pid: p for pid, p in s.players.items() if not p.dropped}
    if s.present_count() < s.config.min_players:
        _err(out, ev.player_id, "not_enough_players", "not enough players present")
        return
    if not s.remaining_categories():
        _err(out, ev.player_id, "no_questions", "no questions were drawn")
        return
    # The XP spread is picked by the number of players present at Start
    # (§2.1 balance rationale); players absent but not dropped still play
    # and draw from the same choices.
    choices = s.config.choices_for(s.present_count())
    for p in s.players.values():
        p.starting_xp = p.xp = rng.choice(choices)
    _start_round(s, now, rng, out)


def _on_pick(s: GameState, ev: Pick, now: int, out: list[Message]) -> None:
    if _player(s, ev.player_id, out) is None:
        return
    if s.phase is not Phase.PICK:
        _err(out, ev.player_id, "wrong_phase", "not the pick phase")
        return
    if ev.player_id != s.picker_id:
        _err(out, ev.player_id, "not_your_pick", "you are not the picker")
        return
    if _too_late(s, now):
        _err(out, ev.player_id, "too_late", "the pick deadline has passed")
        return
    if ev.category_id not in s.board:
        _err(out, ev.player_id, "not_on_board", "that category is not on the board")
        return
    _show_question(s, ev.category_id, now, out)


def _on_answer(s: GameState, ev: Answer, now: int, rng: Rng, out: list[Message]) -> None:
    p = _player(s, ev.player_id, out)
    if p is None:
        return
    if s.phase is Phase.QUESTION:
        question, existing = s.question, s.answers.get(p.id)
    elif s.phase is Phase.BLOCK:
        block = s.blocks.get(p.id)
        if block is None:
            _err(out, p.id, "not_attacked", "you have no block question")
            return
        question, existing = block.question, block.answer
    else:
        _err(out, p.id, "wrong_phase", "no question is open")
        return
    assert question is not None
    if ev.question_id != question.id:
        _err(out, p.id, "wrong_question", "that question is not the open one")
        return
    if existing is not None:
        _err(out, p.id, "already_answered", "you already answered")
        return
    if not 0 <= ev.option < OPTION_COUNT:
        _err(out, p.id, "bad_option", f"option must be 0..{OPTION_COUNT - 1}")
        return
    if _too_late(s, now):
        out.append(AnswerAck(p.id, question.id, accepted=False, reason="too_late"))
        return
    assert s.question_sent_ms is not None
    record = AnswerRecord(
        option=ev.option,
        received_ms=now,
        response_ms=max(0, now - s.question_sent_ms - ev.rtt_ms // 2),
    )
    out.append(AnswerAck(p.id, question.id, accepted=True))
    if s.phase is Phase.QUESTION:
        s.answers[p.id] = record
        if all(q.id in s.answers for q in s.active_players()):
            _reveal(s, now, out)
    else:
        s.blocks[p.id].answer = record
        if all(b.answer is not None for b in s.blocks.values()):
            _resolve_blocks(s, now, rng, out)


def _on_attack(s: GameState, ev: Attack, now: int, rng: Rng, out: list[Message]) -> None:
    p = _player(s, ev.player_id, out)
    if p is None:
        return
    if s.phase is not Phase.ATTACK:
        _err(out, p.id, "wrong_phase", "not the attack phase")
        return
    if _too_late(s, now):
        _err(out, p.id, "too_late", "the attack window has closed")
        return
    if p.id in s.acted_this_window:
        _err(out, p.id, "already_attacked", "one attack or pass per window")
        return
    if p.tokens < 1:
        _err(out, p.id, "no_token", "you hold no attack token")
        return
    if p.xp < 1:
        _err(out, p.id, "no_xp", "you cannot pay for an attack")
        return
    target = s.players.get(ev.target_player_id)
    if target is None or target.dropped:
        _err(out, p.id, "unknown_target", "no such player in the game")
        return
    if target.id == p.id:
        _err(out, p.id, "self_target", "you cannot attack yourself")
        return
    # §2.4: refused at declaration, first by receive time wins; the refused
    # attacker keeps token and XP and may pick another target.
    if s.incoming_attacks(target.id) >= s.config.max_incoming_attacks:
        _err(out, p.id, "target_full", "that player already has enough incoming attacks")
        return
    p.tokens -= 1
    p.score(-s.config.attack_cost)
    s.attacks.append(PendingAttack(p.id, target.id))
    s.acted_this_window.add(p.id)
    out.append(AttackDeclared(p.id, target.id))
    if not s.anyone_awaiting_attack():
        _start_block(s, now, rng, out)


def _on_pass(s: GameState, ev: Pass, now: int, rng: Rng, out: list[Message]) -> None:
    p = _player(s, ev.player_id, out)
    if p is None:
        return
    if s.phase is not Phase.ATTACK:
        _err(out, p.id, "wrong_phase", "not the attack phase")
        return
    if _too_late(s, now):
        _err(out, p.id, "too_late", "the attack window has closed")
        return
    if p.id in s.acted_this_window:
        _err(out, p.id, "already_attacked", "one attack or pass per window")
        return
    if p.tokens < 1:
        _err(out, p.id, "no_token", "you hold no attack token")
        return
    s.acted_this_window.add(p.id)
    out.append(Passed(p.id))
    if not s.anyone_awaiting_attack():
        _start_block(s, now, rng, out)


def _on_disconnect(s: GameState, ev: Disconnect, now: int, rng: Rng, out: list[Message]) -> None:
    p = s.players.get(ev.player_id)
    if p is None or p.dropped or not p.present:
        return
    p.present = False
    p.absent_since_ms = now
    out.append(PresenceChanged(p.id, "absent"))
    _after_presence_change(s, now, rng, out)


def _on_reconnect(s: GameState, ev: Reconnect, now: int, rng: Rng, out: list[Message]) -> None:
    p = _player(s, ev.player_id, out)
    if p is None or p.present:
        return
    p.present = True
    p.absent_since_ms = None
    p.connected_since_ms = now
    out.append(PresenceChanged(p.id, "returned"))
    _after_presence_change(s, now, rng, out)


def _on_tick(s: GameState, now: int, rng: Rng, out: list[Message]) -> None:
    if s.phase is Phase.END:
        return
    _drop_overdue(s, now, out)
    if s.phase is Phase.LOBBY:
        return
    if (
        s.low_presence_since_ms is not None
        and now - s.low_presence_since_ms >= s.config.abandon_seconds * 1000
    ):
        _end_game(s, "abandoned", out)
        return
    if s.phase_end_ms is None or now < s.phase_end_ms:
        return
    match s.phase:
        case Phase.PICK:
            _show_question(s, rng.choice(s.board), now, out)  # auto-pick
        case Phase.QUESTION:
            _reveal(s, now, out)
        case Phase.REVEAL:
            _start_attack_window(s, now, rng, out)
        case Phase.ATTACK:
            _start_block(s, now, rng, out)
        case Phase.BLOCK:
            _resolve_blocks(s, now, rng, out)


# ---------- presence ----------


def _too_late(s: GameState, now: int) -> bool:
    return s.phase_end_ms is not None and now > s.phase_end_ms


def _drop_overdue(s: GameState, now: int, out: list[Message]) -> None:
    limit = s.config.rejoin_seconds * 1000
    for p in s.players.values():
        if not p.present and not p.dropped and p.absent_since_ms is not None:
            if now - p.absent_since_ms >= limit:
                p.dropped = True
                out.append(PresenceChanged(p.id, "dropped"))


def _after_presence_change(s: GameState, now: int, rng: Rng, out: list[Message]) -> None:
    active = s.active_players()
    # §2.8: host passes to the longest-connected present player.
    host = s.players.get(s.host_id) if s.host_id else None
    if active and (host is None or not host.active):
        s.host_id = min(active, key=lambda p: p.connected_since_ms).id
        out.append(HostChanged(s.host_id))
    # §2.8: an absent picker's pick passes to the next present player.
    if s.phase is Phase.PICK and s.picker_id is not None:
        picker = s.players[s.picker_id]
        if not picker.active and active:
            out.append(BoardShown(_assign_picker(s), tuple(s.board)))
    if s.phase in (Phase.LOBBY, Phase.END):
        return
    if len(active) < 2:
        if s.low_presence_since_ms is None:
            s.low_presence_since_ms = now
    else:
        s.low_presence_since_ms = None
    # Someone leaving can be the last thing an input phase was waiting for.
    if s.phase is Phase.QUESTION and active and all(p.id in s.answers for p in active):
        _reveal(s, now, out)
    elif s.phase is Phase.ATTACK and not s.anyone_awaiting_attack():
        _start_block(s, now, rng, out)


def _assign_picker(s: GameState) -> str:
    """Rotate the pick to the next active player after the cursor (§2.2,
    §2.8). With nobody active the last picker keeps it; the abandon rule
    ends the game before that matters."""
    order = s.turn_order()
    n = len(order)
    for offset in range(1, n + 1):
        idx = (s.picker_cursor + offset) % n
        if order[idx].active:
            s.picker_cursor = idx
            s.picker_id = order[idx].id
            break
    if s.picker_id is None:
        s.picker_cursor = 0
        s.picker_id = order[0].id
    return s.picker_id


# ---------- phase transitions ----------


def _set_phase(
    s: GameState, phase: Phase, now: int, seconds: int, with_grace: bool, out: list[Message]
) -> int:
    """Enter `phase` with a fresh deadline; returns the deadline."""
    s.phase = phase
    s.deadline_ms = now + seconds * 1000
    s.phase_end_ms = s.deadline_ms + (s.config.grace_ms if with_grace else 0)
    out.append(PhaseChanged(phase, s.round, s.deadline_ms, s.phase_end_ms))
    return s.deadline_ms


def _start_round(s: GameState, now: int, rng: Rng, out: list[Message]) -> None:
    s.question = None
    s.question_sent_ms = None
    s.answers = {}
    s.attacks = []
    s.acted_this_window = set()
    s.blocks = {}
    available = s.remaining_categories()
    if s.round >= s.config.question_count or not available:
        _end_game(s, "finished", out)
        return
    s.round += 1
    picker_id = _assign_picker(s)
    # rng.sample keeps a deterministic board for a seeded replay.
    s.board = rng.sample(available, min(s.config.board_size, len(available)))
    _set_phase(s, Phase.PICK, now, s.config.pick_seconds, True, out)
    out.append(BoardShown(picker_id, tuple(s.board)))


def _show_question(s: GameState, category_id: str, now: int, out: list[Message]) -> None:
    question = s.question = s.pools[category_id].pop(0)
    s.question_sent_ms = now
    s.answers = {}
    deadline = _set_phase(s, Phase.QUESTION, now, s.config.question_seconds, True, out)
    out.append(QuestionShown(question.id, category_id, deadline))


def _reveal(s: GameState, now: int, out: list[Message]) -> None:
    """Score the round (§2.3, §2.4 tokens) and open the REVEAL phase."""
    cfg, q = s.config, s.question
    assert q is not None
    outcomes: dict[str, PlayerOutcome] = {}
    for p in s.players.values():
        if p.dropped:
            continue  # out of the turn order: never served, never scored
        ans = s.answers.get(p.id)
        token_earned = False
        if ans is None and not p.present:
            outcome, points = "absent", 0  # no change, streak untouched
        elif ans is None:
            outcome, points = "timeout", cfg.points_timeout
            p.streak = 0
        elif ans.option == q.correct_option:
            outcome, points = "correct", cfg.points_correct
            p.streak += 1
            p.correct_response_ms.append(ans.response_ms)
            if p.streak % cfg.streak_for_token == 0 and p.tokens < cfg.max_tokens:
                p.tokens += 1
                token_earned = True
        else:
            outcome, points = "incorrect", cfg.points_wrong
            p.streak = 0
        p.score(points)
        outcomes[p.id] = PlayerOutcome(
            outcome, points, p.nominal_delta, p.streak, p.tokens, token_earned
        )
        s.serves.append(
            ServeRecord(q.id, p.id, outcome, ans.response_ms if ans else None, s.round, "question")
        )
    _set_phase(s, Phase.REVEAL, now, cfg.reveal_seconds, False, out)
    out.append(Revealed(q.id, q.correct_option, outcomes))


def _start_attack_window(s: GameState, now: int, rng: Rng, out: list[Message]) -> None:
    s.attacks = []
    s.acted_this_window = set()
    if not s.anyone_awaiting_attack():
        _start_round(s, now, rng, out)
        return
    _set_phase(s, Phase.ATTACK, now, s.config.attack_window_seconds, True, out)


def _start_block(s: GameState, now: int, rng: Rng, out: list[Message]) -> None:
    if not s.attacks:
        _start_round(s, now, rng, out)
        return
    s.blocks = {}
    for a in s.attacks:
        block = s.blocks.get(a.target_id)
        if block is None:
            block = s.blocks[a.target_id] = BlockChallenge(a.target_id, [], _draw_block(s, rng))
        block.attacker_ids.append(a.attacker_id)
    s.question_sent_ms = now
    deadline = _set_phase(s, Phase.BLOCK, now, s.config.block_seconds, True, out)
    for block in s.blocks.values():
        if block.question is not None:
            out.append(
                BlockQuestionShown(
                    block.target_id, block.question.id, tuple(block.attacker_ids), deadline
                )
            )
    if all(b.question is None for b in s.blocks.values()):
        _resolve_blocks(s, now, rng, out)  # nothing to ask anyone


def _draw_block(s: GameState, rng: Rng) -> Question | None:
    """Next reserve question; with the reserve empty, an unused question
    from any category pool (difficulty ≥ 2 preferred, so it still plays
    like a block question). None only when nothing at all is left."""
    if s.block_reserve:
        return s.block_reserve.pop(0)
    unused = [q for pool in s.pools.values() for q in pool]
    if not unused:
        return None
    hard = [q for q in unused if q.difficulty >= 2]
    question = rng.choice(hard or unused)
    s.pools[question.category_id].remove(question)
    return question


def _resolve_blocks(s: GameState, now: int, rng: Rng, out: list[Message]) -> None:
    """§2.4 BLOCK: correct blocks everything; wrong/timeout costs
    attack_damage per incoming attack (the real XP floored at 0, the
    nominal delta in full) and pays each attacker attack_steal regardless
    of how much the target could lose.
    Never touches streaks or tokens. An absent target times out like
    anyone else (§2.8)."""
    cfg = s.config
    for block in s.blocks.values():
        target = s.players[block.target_id]
        q, ans = block.question, block.answer
        if q is None:
            outcome, blocked = "absent", True  # nothing left to ask: nothing to fail
        elif ans is not None:
            outcome = "correct" if ans.option == q.correct_option else "incorrect"
            blocked = outcome == "correct"
        else:
            outcome, blocked = "timeout", False
        damage = steal = 0
        if not blocked:
            damage = cfg.attack_damage * len(block.attacker_ids)
            target.score(-damage)
            steal = cfg.attack_steal
            for attacker_id in block.attacker_ids:
                s.players[attacker_id].score(steal)
        if q is not None:
            s.serves.append(
                ServeRecord(
                    q.id, target.id, outcome, ans.response_ms if ans else None, s.round, "block"
                )
            )
        out.append(
            BlockResolved(
                target.id,
                tuple(block.attacker_ids),
                blocked,
                outcome,
                damage,
                target.nominal_delta,
                steal,
            )
        )
    _start_round(s, now, rng, out)


def _end_game(s: GameState, reason: EndReason, out: list[Message]) -> None:
    ranked = sorted(s.players.values(), key=_rank_key)
    s.results = [
        PlayerResult(
            p.id, p.display_name, p.starting_xp, p.xp, p.delta, p.nominal_delta, p.mean_correct_ms
        )
        for p in ranked
    ]
    winners, tiebreak = _winners(ranked)
    s.phase = Phase.END
    s.end_reason = reason
    s.deadline_ms = s.phase_end_ms = None
    s.picker_id = None
    s.board = []
    out.append(PhaseChanged(Phase.END, s.round, None, None))
    out.append(Ended(reason, tuple(s.results), tuple(p.id for p in winners), tiebreak))


def ended(s: GameState) -> Ended:
    """The Ended fact again for a finished game (a reconnect at END)."""
    assert s.phase is Phase.END and s.results is not None and s.end_reason is not None
    ranked = [s.players[r.player_id] for r in s.results]
    winners, tiebreak = _winners(ranked)
    return Ended(s.end_reason, tuple(s.results), tuple(p.id for p in winners), tiebreak)


def _rank_key(p: Player) -> tuple[int, int, float]:
    """§2.7: highest total, then highest delta, then fastest correct
    answers (no correct answers ranks slowest)."""
    mean = p.mean_correct_ms
    return (-p.xp, -p.delta, float("inf") if mean is None else mean)


def _winners(ranked: list[Player]) -> tuple[list[Player], Tiebreak]:
    if not ranked:
        return [], None
    top = ranked[0]
    by_total = [p for p in ranked if p.xp == top.xp]
    if len(by_total) == 1:
        return by_total, None
    by_delta = [p for p in by_total if p.delta == top.delta]
    if len(by_delta) == 1:
        return by_delta, "delta"
    key = _rank_key(top)
    by_time = [p for p in by_delta if _rank_key(p) == key]
    if len(by_time) == 1:
        return by_time, "response_time"
    return by_time, "shared"

