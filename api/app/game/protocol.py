"""The WebSocket protocol (spec §4) and the per-recipient serializers.

Two halves:

* **Inbound** — one Pydantic model per client message (`sync`, `start`,
  `pick`, `answer`, `attack`, `pass`, `report`), a discriminated union
  on `type`, and `to_event` turning one into an engine event. `sync` and
  `report` are not game events: the runtime answers them itself.

* **Outbound** — one model per server message, and `fan_out`, which
  takes the engine's messages plus the state they produced and returns,
  per player, exactly what that player may see. `state_message` builds
  the reconnect snapshot under the same rules. Nothing else builds
  client-facing payloads, so §1 rule 3 is enforced in one place:

  - `correct_option` leaves the server only in `reveal`, after the
    QUESTION phase has closed. Block questions are never revealed at
    all: `block_result` says blocked or not.
  - `starting_xp` and XP totals leave only in `end` (and in `state` once
    the game has ended). During the game everyone sees deltas (§2.6) —
    always the *nominal* delta (`Player.nominal_delta`, the floor
    ignored), because a real delta that stops falling would say "at 0"
    and, with the deltas public, give the start away. `end` shows both.
  - Token counts and streaks are only ever the recipient's own
    (`reveal.tokens`, `state.you`). The `lobby`/`state` player lists
    carry no tokens.
  - A pass is acknowledged to the passer only (`pass_ack`); no message
    to anyone else says who passed or who still holds a token. Attacks
    are public (who attacked whom), the target's damage and new delta go
    to the target, each attacker's own gain to that attacker.
  - The config shown to clients (`lobby.config`, `state.config`) omits
    the starting-XP choices and tiers, so no message before `end`
    contains a starting_xp key at all — that keeps the leak tests
    key-based and simple.

The engine speaks in ids; the question text comes from the draw's cache
(`texts_from_cache`) and is looked up here.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.game import engine as eng
from app.game.config import GameConfig
from app.game.engine import GameState, Phase
from app.rules import OPTION_COUNT
from app.schemas.sessions import SessionQuestionsCache

Option = Annotated[int, Field(ge=0, le=OPTION_COUNT - 1)]

# GameConfig fields clients may see. Everything about the secret start
# stays out (see the module docstring).
PRIVATE_CONFIG_FIELDS = frozenset({"starting_xp_choices", "starting_xp_tiers"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- question text ----------


class QuestionText(_Strict):
    """Stem and options in shown (already shuffled) order — the only
    part of a drawn question a client ever receives."""

    stem: str
    options: list[str] = Field(min_length=OPTION_COUNT, max_length=OPTION_COUNT)


def texts_from_cache(cache: SessionQuestionsCache) -> dict[str, QuestionText]:
    return {str(q.id): QuestionText(stem=q.stem, options=q.options) for q in cache.questions}


Texts = Mapping[str, QuestionText]


# ---------- inbound (client → server) ----------


class SyncIn(_Strict):
    type: Literal["sync"]
    client_ms: int


class StartIn(_Strict):
    type: Literal["start"]


class PickIn(_Strict):
    type: Literal["pick"]
    category_id: str


class AnswerIn(_Strict):
    type: Literal["answer"]
    question_id: str
    option: Option


class AttackIn(_Strict):
    type: Literal["attack"]
    target_player_id: str


class PassIn(_Strict):
    type: Literal["pass"]


class ReportIn(_Strict):
    type: Literal["report"]
    question_id: str
    reason: str = Field(min_length=1, max_length=500)


ClientMessage = Annotated[
    SyncIn | StartIn | PickIn | AnswerIn | AttackIn | PassIn | ReportIn,
    Field(discriminator="type"),
]
client_message_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


def parse_client_message(raw: str | bytes | dict[str, Any]) -> ClientMessage:
    """Raises pydantic.ValidationError on anything that is not exactly one
    of the client messages."""
    if isinstance(raw, dict):
        return client_message_adapter.validate_python(raw)
    return client_message_adapter.validate_json(raw)


def to_event(msg: ClientMessage, player_id: str, *, rtt_ms: int = 0) -> eng.Event | None:
    """The engine event for a game message; None for `sync` and `report`,
    which the runtime handles without the engine."""
    match msg:
        case StartIn():
            return eng.Start(player_id)
        case PickIn():
            return eng.Pick(player_id, msg.category_id)
        case AnswerIn():
            return eng.Answer(player_id, msg.question_id, msg.option, rtt_ms=rtt_ms)
        case AttackIn():
            return eng.Attack(player_id, msg.target_player_id)
        case PassIn():
            return eng.Pass(player_id)
        case _:
            return None


# ---------- outbound (server → client) ----------


class PlayerView(_Strict):
    """A player as everyone sees them: name, presence, delta once the game
    is on (§2.6). Never tokens, streak, starting XP or total."""

    player_id: str
    display_name: str
    present: bool
    dropped: bool
    is_host: bool
    delta: int | None  # nominal; None in the lobby


class SyncReply(_Strict):
    type: Literal["sync_reply"] = "sync_reply"
    client_ms: int
    server_ms: int


class LobbyOut(_Strict):
    """The roster: sent on every join and host change (also mid-game, when
    the host seat moves — §2.8)."""

    type: Literal["lobby"] = "lobby"
    players: list[PlayerView]
    host_id: str | None
    config: dict[str, Any]


class PhaseOut(_Strict):
    type: Literal["phase"] = "phase"
    phase: Phase
    round: int
    deadline_ms: int | None  # what to count down to (§5); the grace is server-side


class BoardOut(_Strict):
    type: Literal["board"] = "board"
    picker_id: str
    category_ids: list[str]


class QuestionOut(_Strict):
    type: Literal["question"] = "question"
    question_id: str
    category_id: str
    stem: str
    options: list[str]
    deadline_ms: int


class AnswerAckOut(_Strict):
    type: Literal["answer_ack"] = "answer_ack"
    question_id: str
    accepted: bool
    reason: str | None = None


class PassAckOut(_Strict):
    """To the passer only."""

    type: Literal["pass_ack"] = "pass_ack"


class RevealOut(_Strict):
    """`correct_option` for the round question just closed; the recipient's
    own outcome, streak and tokens; everyone's (nominal) deltas."""

    type: Literal["reveal"] = "reveal"
    question_id: str
    correct_option: Option
    outcome: eng.Outcome | None  # None for a dropped recipient (not served)
    points: int
    delta: int
    streak: int
    tokens: int
    token_earned: bool
    deltas: dict[str, int]


class AttackView(_Strict):
    attacker_id: str
    target_id: str


class AttacksOut(_Strict):
    """Every attack declared so far this window (§4: who attacked whom, no
    XP values). Sent on each declaration."""

    type: Literal["attacks"] = "attacks"
    attacks: list[AttackView]


class BlockQuestionOut(_Strict):
    """To the attacked player only."""

    type: Literal["block_question"] = "block_question"
    question_id: str
    stem: str
    options: list[str]
    attacker_ids: list[str]
    deadline_ms: int


class BlockResultOut(_Strict):
    """Blocked or not, to everyone. The target also sees the nominal
    damage and their new nominal delta; each attacker sees their own gain."""

    type: Literal["block_result"] = "block_result"
    target_id: str
    attacker_ids: list[str]
    blocked: bool
    damage: int | None = None
    delta: int | None = None
    gained: int | None = None


class PresenceOut(_Strict):
    type: Literal["presence"] = "presence"
    player_id: str
    status: Literal["absent", "returned", "dropped"]


class ResultView(_Strict):
    player_id: str
    display_name: str
    starting_xp: int
    final_xp: int
    delta: int  # real: final_xp - starting_xp
    nominal_delta: int  # what everyone watched during the game
    mean_correct_ms: float | None


class EndOut(_Strict):
    type: Literal["end"] = "end"
    reason: eng.EndReason
    results: list[ResultView]  # ranked, winners first
    winner_ids: list[str]
    tiebreak: eng.Tiebreak


class ErrorOut(_Strict):
    type: Literal["error"] = "error"
    code: str
    message: str


class YouView(_Strict):
    """The recipient's private slice of the state."""

    tokens: int
    streak: int
    answered: bool  # the open round/block question, if any
    acted: bool  # attacked or passed in the open attack window


class StateOut(_Strict):
    """Reconnect snapshot: what the recipient would know had they seen
    every message so far, built under the same rules."""

    type: Literal["state"] = "state"
    phase: Phase
    round: int
    deadline_ms: int | None
    players: list[PlayerView]
    host_id: str | None
    picker_id: str | None
    board: list[str]
    question: QuestionOut | None
    block_question: BlockQuestionOut | None
    attacks: list[AttackView]
    you: YouView | None  # None when the recipient is not in the game
    config: dict[str, Any]
    end: EndOut | None


ServerMessage = (
    SyncReply
    | LobbyOut
    | PhaseOut
    | BoardOut
    | QuestionOut
    | AnswerAckOut
    | PassAckOut
    | RevealOut
    | AttacksOut
    | BlockQuestionOut
    | BlockResultOut
    | PresenceOut
    | EndOut
    | ErrorOut
    | StateOut
)

Outbox = dict[str, list[ServerMessage]]  # by recipient player id


# ---------- serializers ----------


def public_config(config: GameConfig) -> dict[str, Any]:
    return {k: v for k, v in config.summary().items() if k not in PRIVATE_CONFIG_FIELDS}


def player_views(s: GameState) -> list[PlayerView]:
    started = s.phase is not Phase.LOBBY
    return [
        PlayerView(
            player_id=p.id,
            display_name=p.display_name,
            present=p.present,
            dropped=p.dropped,
            is_host=p.id == s.host_id,
            delta=p.nominal_delta if started else None,
        )
        for p in s.turn_order()
    ]


def _lobby(s: GameState) -> LobbyOut:
    return LobbyOut(players=player_views(s), host_id=s.host_id, config=public_config(s.config))


def _question(q: eng.Question, deadline_ms: int, texts: Texts) -> QuestionOut:
    text = texts[q.id]
    return QuestionOut(
        question_id=q.id,
        category_id=q.category_id,
        stem=text.stem,
        options=text.options,
        deadline_ms=deadline_ms,
    )


def _block_question(
    question_id: str, attacker_ids: Iterable[str], deadline_ms: int, texts: Texts
) -> BlockQuestionOut:
    text = texts[question_id]
    return BlockQuestionOut(
        question_id=question_id,
        stem=text.stem,
        options=text.options,
        attacker_ids=list(attacker_ids),
        deadline_ms=deadline_ms,
    )


def _attacks(s: GameState) -> list[AttackView]:
    return [AttackView(attacker_id=a.attacker_id, target_id=a.target_id) for a in s.attacks]


def _end(m: eng.Ended) -> EndOut:
    return EndOut(
        reason=m.reason,
        results=[
            ResultView(
                player_id=r.player_id,
                display_name=r.display_name,
                starting_xp=r.starting_xp,
                final_xp=r.final_xp,
                delta=r.delta,
                nominal_delta=r.nominal_delta,
                mean_correct_ms=r.mean_correct_ms,
            )
            for r in m.results
        ],
        winner_ids=list(m.winner_ids),
        tiebreak=m.tiebreak,
    )


def _reveal_for(s: GameState, recipient: str, m: eng.Revealed) -> RevealOut:
    """A dropped recipient was not served (no outcome); their own counters
    are still their own."""
    own = m.outcomes.get(recipient)
    p = s.players[recipient]
    return RevealOut(
        question_id=m.question_id,
        correct_option=m.correct_option,
        outcome=own.outcome if own else None,
        points=own.points if own else 0,
        delta=p.nominal_delta,
        streak=p.streak,
        tokens=p.tokens,
        token_earned=own.token_earned if own else False,
        deltas={pid: o.delta for pid, o in m.outcomes.items()},
    )


def _block_result_for(recipient: str, m: eng.BlockResolved) -> BlockResultOut:
    out = BlockResultOut(target_id=m.target_id, attacker_ids=list(m.attacker_ids), blocked=m.blocked)
    if recipient == m.target_id:
        out.damage, out.delta = m.damage, m.delta
    elif recipient in m.attacker_ids:
        out.gained = 0 if m.blocked else m.steal
    return out


def fan_out(s: GameState, messages: Iterable[eng.Message], texts: Texts) -> Outbox:
    """Per player, the messages they may see for one engine step. `s` is
    the state the step produced. Every player in the game gets an entry
    (possibly empty); the runtime delivers to whoever is connected."""
    recipients = list(s.players)
    out: Outbox = {pid: [] for pid in recipients}
    lobby_sent = False  # one snapshot of the final state per step is enough

    def everyone(msg: ServerMessage) -> None:
        for pid in recipients:
            out[pid].append(msg)

    def only(pid: str, msg: ServerMessage) -> None:
        out.setdefault(pid, []).append(msg)  # a refused joiner is not in s.players

    for m in messages:
        match m:
            case eng.Error():
                only(m.player_id, ErrorOut(code=m.code, message=m.message))
            case eng.PlayerJoined() | eng.HostChanged():
                if not lobby_sent:
                    everyone(_lobby(s))
                    lobby_sent = True
            case eng.PresenceChanged():
                everyone(PresenceOut(player_id=m.player_id, status=m.status))
            case eng.PhaseChanged():
                everyone(PhaseOut(phase=m.phase, round=m.round, deadline_ms=m.deadline_ms))
            case eng.BoardShown():
                everyone(BoardOut(picker_id=m.picker_id, category_ids=list(m.category_ids)))
            case eng.QuestionShown():
                assert s.question is not None and s.question.id == m.question_id
                everyone(_question(s.question, m.deadline_ms, texts))
            case eng.AnswerAck():
                only(
                    m.player_id,
                    AnswerAckOut(question_id=m.question_id, accepted=m.accepted, reason=m.reason),
                )
            case eng.Revealed():
                for pid in recipients:
                    out[pid].append(_reveal_for(s, pid, m))
            case eng.AttackDeclared():
                everyone(AttacksOut(attacks=_attacks(s)))
            case eng.Passed():
                only(m.player_id, PassAckOut())
            case eng.BlockQuestionShown():
                only(
                    m.target_id,
                    _block_question(m.question_id, m.attacker_ids, m.deadline_ms, texts),
                )
            case eng.BlockResolved():
                for pid in recipients:
                    out[pid].append(_block_result_for(pid, m))
            case eng.Ended():
                everyone(_end(m))
            case _:
                raise TypeError(f"no serializer for {m!r}")
    return out


def state_message(s: GameState, recipient: str, texts: Texts) -> StateOut:
    """The reconnect snapshot for one player (§4): the public state plus
    that player's own private slice, nothing of anyone else's."""
    p = s.players.get(recipient)
    you = None
    question = block_question = None
    if p is not None and not p.dropped:
        answered = acted = False
        if s.phase is Phase.QUESTION and s.question is not None:
            assert s.deadline_ms is not None
            question = _question(s.question, s.deadline_ms, texts)
            answered = recipient in s.answers
        elif s.phase is Phase.BLOCK and recipient in s.blocks:
            block = s.blocks[recipient]
            if block.question is not None:
                assert s.deadline_ms is not None
                block_question = _block_question(
                    block.question.id, block.attacker_ids, s.deadline_ms, texts
                )
            answered = block.answer is not None
        elif s.phase is Phase.ATTACK:
            acted = recipient in s.acted_this_window
        you = YouView(tokens=p.tokens, streak=p.streak, answered=answered, acted=acted)
    end = _end(eng.ended(s)) if s.phase is Phase.END else None
    return StateOut(
        phase=s.phase,
        round=s.round,
        deadline_ms=s.deadline_ms,
        players=player_views(s),
        host_id=s.host_id,
        picker_id=s.picker_id,
        board=list(s.board),
        question=question,
        block_question=block_question,
        attacks=_attacks(s) if s.phase in (Phase.ATTACK, Phase.BLOCK) else [],
        you=you,
        config=public_config(s.config),
        end=end,
    )
