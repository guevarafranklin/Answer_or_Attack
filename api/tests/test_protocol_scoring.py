"""Scoring through the protocol (§4, §6): the option a player sees as
correct is the one that scores correct. The draw shuffles options per
session (`option_order`), the `question` message shows them shuffled,
and `answer.option` is an index into what was shown — so across many
seeded shuffles, tapping the displayed correct text must always come
back `correct`, any other displayed option `incorrect`, and `reveal`'s
`correct_option` must point at the text that is actually correct.

Played over WebSocket clients against the running app (tests/asgi_ws.py)
with the real runtime, draw and serializers in the loop.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import select

from app.models import Question, QuestionTranslation
from app.rules import OPTION_COUNT
from app.services import sessions as svc
from tests.test_runtime import Bot, World, live, three_seats  # noqa: F401 - fixture

SEEDS = range(1, 13)
TIMERS = {"pick_seconds": 1, "question_seconds": 2, "reveal_seconds": 1, "attack_window_seconds": 1}


class Tapper(Bot):
    """Taps the displayed option `offset` places after the one whose text
    is the correct answer (offset 0: the correct one). Never attacks, so
    the game is nothing but round questions."""

    def __init__(self, bot: Bot, offset: int, key: dict[str, str]) -> None:
        super().__init__(bot.world, bot.code, bot.player_id, bot.token)
        self.ws = bot.ws
        self.offset, self.key = offset, key
        self.tapped: dict[str, tuple[int, int]] = {}  # question id -> (displayed correct, tapped)
        self.reveals: list[dict[str, Any]] = []

    async def react(self, m: dict[str, Any]) -> None:
        assert self.ws is not None
        match m["type"]:
            case "ping":
                await self.ws.send({"type": "pong", "server_ms": m["server_ms"]})
            case "lobby" | "state":
                if m["type"] == "state" and m["end"]:
                    self.end = m["end"]
            case "end":
                self.end = m
            case "board" if m["picker_id"] == self.player_id:
                await self.ws.send({"type": "pick", "category_id": m["category_ids"][0]})
            case "question":
                assert "correct" not in str(m)
                shown = m["options"].index(self.key[m["question_id"]])
                tap = (shown + self.offset) % OPTION_COUNT
                self.tapped[m["question_id"]] = (shown, tap)
                await self.ws.send({"type": "answer", "question_id": m["question_id"], "option": tap})
            case "reveal":
                self.reveals.append(m)
            case "phase" if m["phase"] == "attack":
                await self.ws.send({"type": "pass"})


async def answer_key(world: World) -> dict[str, str]:
    """Question id -> the text of its correct option (the bank's
    `correct_index` applied to the unshuffled `en` options)."""
    async with world.factory() as s:
        rows = await s.execute(
            select(Question.id, Question.correct_index, QuestionTranslation.options)
            .join(QuestionTranslation, QuestionTranslation.question_id == Question.id)
            .where(Question.category_id.in_(world.category_ids), QuestionTranslation.locale == "en")
        )
        return {str(qid): options[correct] for qid, correct, options in rows}


@pytest.mark.asyncio
async def test_the_displayed_correct_option_is_the_one_that_scores(live: World, monkeypatch: pytest.MonkeyPatch):
    seeds = iter(SEEDS)
    monkeypatch.setattr(svc, "new_seed", lambda: next(seeds))
    shown_positions: set[int] = set()
    seen_seeds: list[int] = []

    for _ in SEEDS:
        code, seats = await three_seats(live, question_count=3, **TIMERS)
        key = await answer_key(live)
        bots = [Tapper(b, offset, key) for offset, b in enumerate(seats)]
        host = bots[0].ws
        assert host is not None
        await host.recv_type("state")
        await host.send({"type": "start"})
        ends = await asyncio.gather(*(b.play() for b in bots))
        assert all(e["reason"] == "finished" for e in ends)
        row = await live.session(code)
        assert row.rng_seed is not None
        seen_seeds.append(row.rng_seed)

        for b in bots:
            assert len(b.reveals) == 3 and len(b.tapped) == 3
            for r in b.reveals:
                shown, tap = b.tapped[r["question_id"]]
                shown_positions.add(shown)
                # The reveal points at the option whose text is correct...
                assert r["correct_option"] == shown, (row.rng_seed, r, b.tapped)
                # ...and the outcome follows what this player tapped.
                expected = "correct" if b.offset == 0 else "incorrect"
                assert r["outcome"] == expected, (row.rng_seed, b.offset, tap, r)
                assert r["points"] == (3 if expected == "correct" else -1)
            assert all(m["accepted"] for m in b.ws.log if m["type"] == "answer_ack")  # type: ignore[union-attr]

        # Every seat's outcome persisted the same way it was shown.
        results = {r["player_id"]: r for r in ends[0]["results"]}
        assert results[bots[0].player_id]["nominal_delta"] == 9
        assert results[bots[1].player_id]["nominal_delta"] == -3
        assert results[bots[2].player_id]["nominal_delta"] == -3

    assert seen_seeds == list(SEEDS)
    # The shuffles really moved the correct option around.
    assert shown_positions == set(range(OPTION_COUNT)), shown_positions


@pytest.mark.asyncio
async def test_the_option_index_is_into_the_shuffled_list_not_the_banks(live: World, monkeypatch: pytest.MonkeyPatch):
    """The bank's `correct_index` is not what the client answers with:
    where the shuffle moved the correct text, tapping the bank's index
    scores incorrect."""
    monkeypatch.setattr(svc, "new_seed", lambda: 7)
    _, seats = await three_seats(live, question_count=3, **TIMERS)
    key = await answer_key(live)
    async with live.factory() as s:
        bank_index = {
            str(qid): idx
            for qid, idx in await s.execute(
                select(Question.id, Question.correct_index).where(Question.category_id.in_(live.category_ids))
            )
        }

    class BankTapper(Tapper):
        async def react(self, m: dict[str, Any]) -> None:
            if m["type"] == "question":
                shown = m["options"].index(self.key[m["question_id"]])
                tap = bank_index[m["question_id"]]
                self.tapped[m["question_id"]] = (shown, tap)
                await self.ws.send({"type": "answer", "question_id": m["question_id"], "option": tap})  # type: ignore[union-attr]
                return
            await super().react(m)

    bots = [BankTapper(seats[0], 0, key), Tapper(seats[1], 1, key), Tapper(seats[2], 2, key)]
    host = bots[0].ws
    assert host is not None
    await host.recv_type("state")
    await host.send({"type": "start"})
    await asyncio.gather(*(b.play() for b in bots))

    moved = 0
    for r in bots[0].reveals:
        shown, tap = bots[0].tapped[r["question_id"]]
        assert r["outcome"] == ("correct" if tap == shown else "incorrect")
        moved += tap != shown
    assert moved >= 1, "seed 7 left every correct option in its bank position; pick another seed"
