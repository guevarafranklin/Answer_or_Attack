"""§9 step 5, the Claude generator backend. Nothing here touches the network:
the SDK is pointed at an httpx2 MockTransport that replays recorded Messages
API responses, so the real request encoding, error mapping and usage
parsing are exercised end to end."""
import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.schemas.generation import GenerationParams
from app.services import claude_generator as cg
from app.services.claude_generator import ClaudeGenerator, build_user_prompt
from app.services.generator import GeneratedChunk, GeneratorError, Usage, get_generator
from app.workers.generation import CHUNK_SIZE
from tests.test_generation import _category, _job, _questions_for, _run
from tests.test_validation import item

MODEL = "claude-test-model"
PRICE_IN, PRICE_OUT = 3.0, 15.0  # USD per MTok

# A recorded Messages API response: two valid bilingual items.
RECORDED_QUESTIONS = [
    item(**{"en.stem": "What is 2 + 2?", "es.stem": "¿Cuánto es 2 + 2?"}),
    item(
        **{
            "tags": ["solar system"],
            "correct_index": 1,
            "en.stem": "Which planet is largest?",
            "en.options": ["Saturn", "Jupiter", "Neptune", "Earth"],
            "es.stem": "¿Cuál es el planeta más grande?",
            "es.options": ["Saturno", "Júpiter", "Neptuno", "Tierra"],
        }
    ),
]


def message_body(text: str, *, input_tokens=1000, output_tokens=2000, stop_reason="end_turn") -> dict:
    return {
        "id": "msg_01recorded",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def ok(questions=RECORDED_QUESTIONS, **kw) -> httpx2.Response:
    return httpx2.Response(200, json=message_body(json.dumps({"questions": questions}), **kw))


def api_error(status: int, error_type: str = "api_error", **headers: str) -> httpx2.Response:
    return httpx2.Response(
        status, json={"type": "error", "error": {"type": error_type, "message": "recorded"}}, headers=headers
    )


class Timeout:
    """A step that makes the transport time out instead of answering."""


class Replay:
    """MockTransport handler replaying `steps` in order; records each request body."""

    def __init__(self, *steps: httpx2.Response | Timeout):
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        assert self.steps, "more requests than recorded steps"
        step = self.steps.pop(0)
        if isinstance(step, Timeout):
            raise httpx2.ReadTimeout("recorded timeout", request=request)
        return step


class FakeSleep:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_generator(*steps, structured_output=True, retry_delay=5.0) -> tuple[ClaudeGenerator, Replay, FakeSleep]:
    replay = Replay(*steps)
    client = anthropic.AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(replay)),
    )
    sleep = FakeSleep()
    gen = ClaudeGenerator(
        client, MODEL, PRICE_IN, PRICE_OUT,
        structured_output=structured_output, retry_delay=retry_delay, sleep=sleep,
    )
    return gen, replay, sleep


def params(**kw) -> GenerationParams:
    return GenerationParams(category_slug="science", count=40, **kw)


def expected_cents(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * PRICE_IN + output_tokens * PRICE_OUT) / 1_000_000 * 100


# ---------- happy path ----------


@pytest.mark.asyncio
async def test_happy_path_returns_items_and_usage():
    gen, replay, sleep = make_generator(ok(input_tokens=1234, output_tokens=5678))
    chunk = await gen.generate(params(), 2, 0)

    assert isinstance(chunk, GeneratedChunk)
    assert chunk.items == RECORDED_QUESTIONS
    assert chunk.usage == Usage(1234, 5678, expected_cents(1234, 5678))
    assert len(replay.requests) == 1
    assert sleep.calls == []
    assert gen.name == MODEL


@pytest.mark.asyncio
async def test_request_asks_for_strict_json_in_one_call():
    gen, replay, _ = make_generator(ok())
    await gen.generate(params(), 20, 0)

    [req] = replay.requests
    assert req["model"] == MODEL
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": cg.RESPONSE_SCHEMA}
    assert req["max_tokens"] >= 20 * cg.MAX_TOKENS_PER_ITEM
    assert [m["role"] for m in req["messages"]] == ["user"]
    system = req["system"]
    # §5.3 guidance and the §5.2 rules live in the system prompt.
    for phrase in (
        "one defensible correct answer",
        "same category of thing",
        "all of the above",
        "time_sensitive",
        "not a transliteration",
        "Mexican and Central American",
        "at most 120 characters",
        "at most 60 characters",
        "Exactly 4 options",
    ):
        assert phrase in system, phrase
    # Both locales in one call, in the §5.2 shape.
    assert '"en": {"stem"' in system and '"es": {"stem"' in system
    user = req["messages"][0]["content"]
    assert user.startswith("Write exactly 20 questions for the category: science.")
    assert "provide both en and es" in user


@pytest.mark.asyncio
async def test_structured_output_can_be_turned_off():
    gen, replay, _ = make_generator(ok(), structured_output=False)
    await gen.generate(params(), 2, 0)
    assert "output_config" not in replay.requests[0]


def test_user_prompt_carries_job_params_and_avoid_hints():
    p = params(
        difficulty_min=2, difficulty_max=4, grade_bands=["g4_g6", "g7_g9"], region="latam",
        style_notes="no calculator required",
    )
    prompt = build_user_prompt(p, 20, avoid=["algebra: 4", "solar system: Jupiter", "algebra: 4"])

    assert "from 2 to 4 inclusive" in prompt
    assert "grade_band: g4_g6, g7_g9" in prompt
    assert "region: latam" in prompt and cg.REGION_GUIDANCE["latam"] in prompt
    assert "style notes from the editor: no calculator required" in prompt
    # Hints are listed once each, as "don't repeat" guidance.
    assert prompt.count("- algebra: 4") == 1
    assert "- solar system: Jupiter" in prompt
    assert "Do not repeat these topics" in prompt


def test_user_prompt_defaults():
    prompt = build_user_prompt(params(), 5, avoid=[])
    assert "from 1 to 5 inclusive" in prompt
    assert "grade_band: g1_g3, g4_g6, g7_g9, g10_g12, adult" in prompt
    assert "region: global" in prompt
    assert "style notes" not in prompt
    assert "Do not repeat" not in prompt


def test_avoid_hints_are_capped_to_the_newest():
    hints = [f"topic {i}: answer {i}" for i in range(cg.MAX_AVOID_HINTS + 10)]
    prompt = build_user_prompt(params(), 5, avoid=hints)
    assert "- topic 0: answer 0" not in prompt
    assert f"- topic {len(hints) - 1}: answer {len(hints) - 1}" in prompt
    assert prompt.count("\n- topic ") == cg.MAX_AVOID_HINTS


# ---------- malformed responses ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, stop_reason, expected",
    [
        ('{"questions": [{"difficulty": 3', "max_tokens", "response truncated at max_tokens"),
        ("Sure! Here are the questions:\n{...}", "end_turn", "malformed JSON"),
        ('{"items": []}', "end_turn", 'expected {"questions": [...]}'),
        ('"just a string"', "end_turn", 'expected {"questions": [...]}'),
    ],
)
async def test_malformed_json_fails_the_chunk_without_retry(text, stop_reason, expected):
    body = message_body(text, input_tokens=100, output_tokens=200, stop_reason=stop_reason)
    gen, replay, sleep = make_generator(httpx2.Response(200, json=body))

    with pytest.raises(GeneratorError) as excinfo:
        await gen.generate(params(), 2, 0)

    assert expected in str(excinfo.value)
    assert excinfo.value.usage == Usage(100, 200, expected_cents(100, 200))  # tokens were spent
    assert len(replay.requests) == 1  # no retry for a bad response
    assert sleep.calls == []


@pytest.mark.asyncio
async def test_response_without_text_fails_the_chunk():
    body = message_body("", stop_reason="refusal")
    body["content"] = []
    gen, replay, _ = make_generator(httpx2.Response(200, json=body))
    with pytest.raises(GeneratorError, match="no text in response .*refusal"):
        await gen.generate(params(), 2, 0)
    assert len(replay.requests) == 1


# ---------- transient errors ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "step",
    [Timeout(), api_error(429, "rate_limit_error"), api_error(500), api_error(529, "overloaded_error")],
    ids=["timeout", "429", "500", "529"],
)
async def test_transient_error_is_retried_once_then_succeeds(step):
    gen, replay, sleep = make_generator(step, ok(), retry_delay=7.0)
    chunk = await gen.generate(params(), 2, 0)

    assert chunk.items == RECORDED_QUESTIONS
    assert len(replay.requests) == 2
    assert sleep.calls == [7.0]  # backed off exactly once
    assert replay.requests[0] == replay.requests[1]  # same prompt both times


@pytest.mark.asyncio
async def test_retry_after_header_stretches_the_backoff():
    gen, _, sleep = make_generator(api_error(429, "rate_limit_error", **{"retry-after": "12"}), ok(), retry_delay=5.0)
    await gen.generate(params(), 2, 0)
    assert sleep.calls == [12.0]


@pytest.mark.asyncio
async def test_transient_error_twice_fails_the_chunk():
    gen, replay, sleep = make_generator(api_error(503), api_error(503))
    with pytest.raises(GeneratorError, match="Error code: 503"):
        await gen.generate(params(), 2, 0)
    assert len(replay.requests) == 2  # one retry, not more
    assert len(sleep.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422], ids=str)
async def test_non_transient_api_error_is_not_retried(status):
    gen, replay, sleep = make_generator(api_error(status, "invalid_request_error"))
    with pytest.raises(GeneratorError):
        await gen.generate(params(), 2, 0)
    assert len(replay.requests) == 1
    assert sleep.calls == []


# ---------- selection ----------


def test_get_generator_claude_needs_an_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "generator_backend", "claude")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        get_generator()


def test_get_generator_claude_uses_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "generator_backend", "claude")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "generator_model", "claude-configured")
    gen = get_generator()
    assert isinstance(gen, ClaudeGenerator)
    assert gen.name == "claude-configured"
    assert gen._client.max_retries == 0  # our retry policy, not the SDK's


# ---------- through the worker ----------


@pytest.mark.asyncio
async def test_worker_records_cost_and_model_from_real_usage(db: AsyncSession):
    """Two chunks, two recorded responses: cost_cents is the priced sum of
    the returned usage, rounded, and `model` is the generator's model."""
    await _category(db, "science")
    job = await _job(db, CHUNK_SIZE + 2, slug="science")
    gen, replay, _ = make_generator(
        ok(input_tokens=10_000, output_tokens=20_000),  # 3¢ + 30¢
        ok([item(**{"en.stem": "What is 3 + 3?"})], input_tokens=5_000, output_tokens=1_000),  # 1.5¢ + 1.5¢
    )
    job = await _run(db, job, gen)

    assert job.model == MODEL
    assert job.cost_cents == round(expected_cents(15_000, 21_000)) == 36
    assert (job.stats["input_tokens"], job.stats["output_tokens"]) == (15_000, 21_000)
    assert job.status == "succeeded"
    assert job.accepted_count == 3
    assert len(await _questions_for(db, job)) == 3
    assert all(q.status == "pending" for q in await _questions_for(db, job))
    # The second call was told what the first one produced.
    hints = replay.requests[1]["messages"][0]["content"]
    assert "- algebra: 4" in hints and "- solar system: Jupiter" in hints
    assert "Do not repeat" not in replay.requests[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_worker_keeps_cost_of_a_malformed_chunk(db: AsyncSession):
    await _category(db, "science")
    job = await _job(db, CHUNK_SIZE + 1, slug="science")
    gen, _, _ = make_generator(
        httpx2.Response(200, json=message_body("not json", input_tokens=4_000, output_tokens=2_000)),
        ok(input_tokens=1_000, output_tokens=1_000),
    )
    job = await _run(db, job, gen)

    assert job.status == "partial"
    assert job.stats["chunks_failed"] == 1
    assert job.stats["chunk_errors"] == ["chunk 0: GeneratorError: malformed JSON: Expecting value: line 1 column 1 (char 0)"]
    assert job.accepted_count == 2
    assert (job.stats["input_tokens"], job.stats["output_tokens"]) == (5_000, 3_000)
    assert job.cost_cents == round(expected_cents(5_000, 3_000)) == 6
