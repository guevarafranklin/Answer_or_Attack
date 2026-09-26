"""§9 step 9b: POST /admin/generate/parse and app.services.prompt_parser.
Like test_claude_generator, nothing touches the network: the SDK runs on an
httpx2 MockTransport replaying recorded Messages API responses, so the real
request encoding (structured output config, prompt contents) is what is
asserted on."""
import json
from typing import Any

import anthropic
import httpx2
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.routers.generation import get_prompt_parser
from app.schemas.generation import MAX_JOB_COUNT
from app.services import prompt_parser as pp
from app.services.prompt_parser import ParseError, PromptParser, normalize
from tests.test_generation import _category

MODEL = "claude-test-model"
SLUGS = ["science", "world-history", "math"]


def answer(**overrides: Any) -> dict[str, Any]:
    """A recorded model answer in the RESPONSE_SCHEMA shape."""
    base = {
        "category_slug": "science",
        "category_mentioned": "science",
        "count": 40,
        "grade_bands": ["g10_g12"],
        "difficulty_min": 2,
        "difficulty_max": 4,
        "region": "global",
        "locales": ["en", "es"],
        "style_notes": "",
    }
    return {**base, **overrides}


def message_body(text: str, stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_01parse",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 500, "output_tokens": 80},
    }


def ok(data: dict[str, Any] | None = None, *, text: str | None = None) -> httpx2.Response:
    return httpx2.Response(200, json=message_body(text if text is not None else json.dumps(data or answer())))


def api_error(status: int) -> httpx2.Response:
    return httpx2.Response(
        status, json={"type": "error", "error": {"type": "api_error", "message": "recorded"}}
    )


class Replay:
    def __init__(self, *steps: httpx2.Response):
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        assert self.steps, "more requests than recorded steps"
        return self.steps.pop(0)


def make_parser(*steps: httpx2.Response, structured_output=True) -> tuple[PromptParser, Replay]:
    replay = Replay(*steps)
    client = anthropic.AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(replay)),
    )
    return PromptParser(client, MODEL, structured_output=structured_output), replay


# ---------- the call ----------


@pytest.mark.asyncio
async def test_one_structured_call_with_slugs_and_prompt():
    parser, replay = make_parser(ok())
    parsed = await parser.parse("40 science questions for high schoolers", SLUGS)

    [req] = replay.requests
    assert req["model"] == MODEL
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": pp.RESPONSE_SCHEMA}
    assert req["system"] == pp.SYSTEM_PROMPT
    [msg] = req["messages"]
    assert msg["role"] == "user"
    for slug in SLUGS:
        assert f"- {slug}" in msg["content"]
    assert "40 science questions for high schoolers" in msg["content"]

    p = parsed.params
    assert p.category_slug == "science"
    assert p.count == 40
    assert (p.difficulty_min, p.difficulty_max) == (2, 4)
    assert p.grade_bands == ["g10_g12"]
    assert p.locales == ["en", "es"]
    assert p.style_notes is None
    assert parsed.notes == []


@pytest.mark.asyncio
async def test_structured_output_can_be_turned_off():
    parser, replay = make_parser(ok(), structured_output=False)
    await parser.parse("anything", SLUGS)
    assert "output_config" not in replay.requests[0]


@pytest.mark.asyncio
async def test_no_categories_is_a_422_before_any_call():
    parser, replay = make_parser()
    with pytest.raises(ParseError) as exc_info:
        await parser.parse("science", [])
    assert exc_info.value.status == 422
    assert replay.requests == []


@pytest.mark.asyncio
async def test_api_error_is_502():
    parser, _ = make_parser(api_error(500))
    with pytest.raises(ParseError) as exc_info:
        await parser.parse("science", SLUGS)
    assert exc_info.value.status == 502
    assert "InternalServerError" in str(exc_info.value)


@pytest.mark.asyncio
async def test_non_json_answer_is_502():
    parser, _ = make_parser(ok(text="Sure! Here are the params:"), structured_output=False)
    with pytest.raises(ParseError) as exc_info:
        await parser.parse("science", SLUGS)
    assert exc_info.value.status == 502
    assert "stop_reason=end_turn" in str(exc_info.value)


# ---------- normalisation ----------


def test_unknown_category_lists_valid_slugs():
    with pytest.raises(ParseError) as exc_info:
        normalize(answer(category_slug="", category_mentioned="astronomy"), SLUGS)
    err = exc_info.value
    assert err.status == 422
    assert str(err) == "no category matches 'astronomy'; valid slugs: science, world-history, math"


def test_invented_slug_is_rejected_even_when_plausible():
    with pytest.raises(ParseError, match="no category matches 'astronomy'"):
        normalize(answer(category_slug="astronomy", category_mentioned="space"), SLUGS)


def test_no_category_in_prompt():
    with pytest.raises(ParseError, match="the prompt names no category; valid slugs"):
        normalize(answer(category_slug="", category_mentioned=""), SLUGS)


@pytest.mark.parametrize(
    "count, expected, note",
    [
        (500, MAX_JOB_COUNT, f"count 500 clamped to {MAX_JOB_COUNT}"),
        (0, 1, "count 0 raised to 1"),
        (-3, 1, "count -3 raised to 1"),
        (MAX_JOB_COUNT, MAX_JOB_COUNT, None),
        (1, 1, None),
    ],
)
def test_count_is_clamped_and_noted(count, expected, note):
    parsed = normalize(answer(count=count), SLUGS)
    assert parsed.params.count == expected
    if note:
        assert any(n.startswith(note) for n in parsed.notes), parsed.notes
    else:
        assert parsed.notes == []


def test_difficulty_out_of_scale_is_clamped():
    parsed = normalize(answer(difficulty_min=0, difficulty_max=9), SLUGS)
    assert (parsed.params.difficulty_min, parsed.params.difficulty_max) == (1, 5)
    assert parsed.notes == ["difficulty 0–9 clamped to the 1–5 scale"]


def test_inverted_difficulty_is_swapped():
    parsed = normalize(answer(difficulty_min=4, difficulty_max=2), SLUGS)
    assert (parsed.params.difficulty_min, parsed.params.difficulty_max) == (2, 4)
    assert parsed.notes == ["difficulty range 4–2 was inverted; using 2–4"]


def test_grade_bands_deduped_and_ordered_unknown_dropped():
    parsed = normalize(
        answer(grade_bands=["g7_g9", "g1_g3", "g7_g9", "college", "g10_g12", "g4_g6"]), SLUGS
    )
    assert parsed.params.grade_bands == ["g1_g3", "g4_g6", "g7_g9", "g10_g12"]


def test_first_grade_to_high_school_shape_round_trips():
    """The shape the prompt asks the model to produce for that phrase."""
    parsed = normalize(
        answer(grade_bands=["g1_g3", "g4_g6", "g7_g9", "g10_g12"], difficulty_min=1, difficulty_max=4),
        SLUGS,
    )
    assert parsed.params.grade_bands == ["g1_g3", "g4_g6", "g7_g9", "g10_g12"]
    assert (parsed.params.difficulty_min, parsed.params.difficulty_max) == (1, 4)


def test_locales_default_to_both_and_region_falls_back():
    parsed = normalize(answer(locales=[], region="mars"), SLUGS)
    assert parsed.params.locales == ["en", "es"]
    assert parsed.params.region == "global"
    only_es = normalize(answer(locales=["es", "es"]), SLUGS)
    assert only_es.params.locales == ["es"]


def test_style_notes_blank_becomes_none():
    assert normalize(answer(style_notes="   "), SLUGS).params.style_notes is None
    assert normalize(answer(style_notes="no year questions"), SLUGS).params.style_notes == "no year questions"


# ---------- the endpoint ----------


@pytest.fixture
def parser_steps():
    """Install a transport-mocked parser as the route dependency; yields the
    list of steps to fill and the Replay recording the requests."""
    holder: dict[str, Any] = {}

    def install(*steps: httpx2.Response) -> Replay:
        parser, replay = make_parser(*steps)
        holder["parser"] = parser
        return replay

    app.dependency_overrides[get_prompt_parser] = lambda: holder["parser"]
    try:
        yield install
    finally:
        app.dependency_overrides.pop(get_prompt_parser, None)


@pytest.mark.asyncio
async def test_parse_requires_admin(client: AsyncClient):
    r = await client.post("/admin/generate/parse", json={"prompt": "x"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_parse_returns_params_and_creates_nothing(
    client: AsyncClient, admin_headers, db: AsyncSession, parser_steps
):
    await _category(db, "science")
    await _category(db, "math")
    replay = parser_steps(ok(answer(count=999, style_notes="favour experiments")))

    r = await client.post(
        "/admin/generate/parse", json={"prompt": "999 science questions"}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["params"]["category_slug"] == "science"
    assert body["params"]["count"] == MAX_JOB_COUNT
    assert body["params"]["style_notes"] == "favour experiments"
    assert body["notes"] == [f"count 999 clamped to {MAX_JOB_COUNT}, the maximum for one job"]

    # The prompt listed exactly the categories in the DB, in slug order.
    content = replay.requests[0]["messages"][0]["content"]
    assert "- math\n- science" in content

    jobs = await client.get("/admin/generate", headers=admin_headers)
    assert jobs.json() == []


@pytest.mark.asyncio
async def test_parse_unknown_category_is_422_listing_slugs(
    client: AsyncClient, admin_headers, db: AsyncSession, parser_steps
):
    await _category(db, "science")
    parser_steps(ok(answer(category_slug="", category_mentioned="knitting")))

    r = await client.post("/admin/generate/parse", json={"prompt": "knitting"}, headers=admin_headers)
    assert r.status_code == 422
    assert r.json()["detail"] == "no category matches 'knitting'; valid slugs: science"


@pytest.mark.asyncio
async def test_parse_model_failure_is_502(client: AsyncClient, admin_headers, db: AsyncSession, parser_steps):
    await _category(db, "science")
    parser_steps(api_error(529))
    r = await client.post("/admin/generate/parse", json={"prompt": "science"}, headers=admin_headers)
    assert r.status_code == 502
    assert r.json()["detail"].startswith("model request failed")


@pytest.mark.asyncio
async def test_parse_empty_prompt_is_422(client: AsyncClient, admin_headers, parser_steps):
    parser_steps()
    r = await client.post("/admin/generate/parse", json={"prompt": ""}, headers=admin_headers)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_parse_without_api_key_is_503(client: AsyncClient, admin_headers, monkeypatch: pytest.MonkeyPatch):
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "")
    r = await client.post("/admin/generate/parse", json={"prompt": "science"}, headers=admin_headers)
    assert r.status_code == 503
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]
