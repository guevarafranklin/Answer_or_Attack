"""Free text → §5.1 GenerationParams, behind POST /admin/generate/parse.

One Messages API call with structured outputs turns the admin's request
("40 hard science questions for high schoolers, US audience") into the
fields of GenerationParams. The model chooses; this module only checks:

- the category must be one of the slugs that exist right now (the valid
  list is part of the prompt, and the response is checked against it
  again), otherwise ParseError lists the valid slugs;
- count is clamped to 1..MAX_JOB_COUNT, difficulty to 1..5 with min <= max;
- every such adjustment is reported back in `notes` so the admin sees what
  was changed before confirming.

Nothing here creates a job or touches the database.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import anthropic
from anthropic.types import Message

from app.config import settings
from app.models._common import GRADE_BANDS, LOCALES, REGIONS
from app.schemas.generation import MAX_JOB_COUNT, GenerationParams

log = logging.getLogger(__name__)

DEFAULT_COUNT = 20
MAX_TOKENS = 1024
REQUEST_TIMEOUT_SECONDS = 60.0


class ParseError(Exception):
    """The prompt could not be turned into params. `status` is the HTTP
    status the router should answer with: 422 when the prompt is the
    problem (no matching category), 502 when the model or the API is."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass
class ParsedPrompt:
    params: GenerationParams
    notes: list[str] = field(default_factory=list)


SYSTEM_PROMPT = f"""You turn an editor's free-text request for trivia questions into the parameters of a generation job. Output only the JSON object described by the schema; every field is required.

Fields:
- category_slug: the one slug from the list of valid categories in the request that best matches the subject the editor asked for (match on meaning: "space" → an astronomy or science category). If no valid category fits, output "" — never invent a slug.
- category_mentioned: the subject exactly as the editor phrased it, or "" if they named none.
- count: how many questions the editor asked for; {DEFAULT_COUNT} when not stated. Output the number as asked even if it is very large; it is clamped later.
- grade_bands: the audience, as a list drawn from {", ".join(GRADE_BANDS)}: g1_g3 = grades 1–3 (ages 6–8), g4_g6 = grades 4–6 (ages 9–11), g7_g9 = grades 7–9 (middle school), g10_g12 = grades 10–12 (high school), adult = anyone out of school. A span such as "first grade to high school" means every band from g1_g3 through g10_g12 inclusive; "kids" means g1_g3 and g4_g6; "students" or "school" means g1_g3 through g10_g12; "adults", "pub quiz" or "general audience" means adult. Empty list when the editor gives no audience.
- difficulty_min, difficulty_max: 1 = most adults know it, 2 = high-school level, 3 = an interested amateur knows it, 4 = an enthusiast knows it, 5 = a specialist knows it. Explicit wording wins: easy = 1–2, medium = 2–3, hard = 4–5, "mixed" or "all levels" = 1–5. When only an audience is given, derive the range from the bands: g1_g3 → 1–1, g4_g6 → 1–2, g7_g9 → 2–3, g10_g12 → 2–4, adult → 3–5; for several bands take the lowest minimum and the highest maximum. When neither is given, use 1–5.
- region: one of {", ".join(REGIONS)}: "us" when the editor wants facts specific to the United States, "latam" for Latin America, otherwise "global".
- locales: the languages to write, from {", ".join(LOCALES)}. Both unless the editor asks for only one.
- style_notes: any remaining instruction about tone, topics to include or avoid, question forms, or anything else the question writer should honour, in one or two sentences in English; "" if there is none. Do not repeat what the other fields already capture."""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category_slug": {"type": "string"},
        "category_mentioned": {"type": "string"},
        "count": {"type": "integer"},
        "grade_bands": {"type": "array", "items": {"type": "string", "enum": list(GRADE_BANDS)}},
        "difficulty_min": {"type": "integer"},
        "difficulty_max": {"type": "integer"},
        "region": {"type": "string", "enum": list(REGIONS)},
        "locales": {"type": "array", "items": {"type": "string", "enum": list(LOCALES)}},
        "style_notes": {"type": "string"},
    },
    "required": [
        "category_slug",
        "category_mentioned",
        "count",
        "grade_bands",
        "difficulty_min",
        "difficulty_max",
        "region",
        "locales",
        "style_notes",
    ],
    "additionalProperties": False,
}


def build_user_prompt(prompt: str, slugs: list[str]) -> str:
    return "\n".join(
        [
            "Valid category slugs (pick exactly one, or \"\" if none fits):",
            *(f"- {s}" for s in slugs),
            "",
            "Editor's request:",
            prompt.strip(),
        ]
    )


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _as_int(value: Any, default: int) -> int:
    """Structured outputs guarantee an integer; the fallback path does not."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize(data: dict[str, Any], slugs: list[str]) -> ParsedPrompt:
    """Turn the model's answer into GenerationParams, clamping and noting.
    Raises ParseError(422) when the category does not match a valid slug."""
    notes: list[str] = []
    valid = ", ".join(slugs)

    slug = str(data.get("category_slug") or "").strip()
    if slug not in slugs:
        mentioned = str(data.get("category_mentioned") or "").strip()
        what = f"no category matches {mentioned!r}" if mentioned else "the prompt names no category"
        if slug:
            what = f"no category matches {slug!r}"
        raise ParseError(f"{what}; valid slugs: {valid}", status=422)

    count = _as_int(data.get("count"), DEFAULT_COUNT)
    if count < 1:
        notes.append(f"count {count} raised to 1")
        count = 1
    elif count > MAX_JOB_COUNT:
        notes.append(f"count {count} clamped to {MAX_JOB_COUNT}, the maximum for one job")
        count = MAX_JOB_COUNT

    dmin = _as_int(data.get("difficulty_min"), 1)
    dmax = _as_int(data.get("difficulty_max"), 5)
    if not (1 <= dmin <= 5 and 1 <= dmax <= 5):
        notes.append(f"difficulty {dmin}–{dmax} clamped to the 1–5 scale")
        dmin, dmax = _clamp(dmin, 1, 5), _clamp(dmax, 1, 5)
    if dmin > dmax:
        notes.append(f"difficulty range {dmin}–{dmax} was inverted; using {dmax}–{dmin}")
        dmin, dmax = dmax, dmin

    bands = [b for b in data.get("grade_bands") or [] if b in GRADE_BANDS]
    bands = sorted(set(bands), key=GRADE_BANDS.index)

    region = data.get("region") if data.get("region") in REGIONS else "global"

    locales = [loc for loc in data.get("locales") or [] if loc in LOCALES]
    locales = sorted(set(locales), key=LOCALES.index) or list(LOCALES)

    style = str(data.get("style_notes") or "").strip() or None

    params = GenerationParams(
        category_slug=slug,
        count=count,
        difficulty_min=dmin,
        difficulty_max=dmax,
        grade_bands=bands,
        region=region,
        locales=locales,
        style_notes=style,
    )
    return ParsedPrompt(params, notes)


class PromptParser:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        model: str,
        structured_output: bool = True,
    ):
        self._client = client
        self.model = model
        self._structured_output = structured_output

    @classmethod
    def from_settings(cls) -> "PromptParser":
        if not settings.anthropic_api_key:
            raise ParseError("prompt parsing needs ANTHROPIC_API_KEY on the API server", status=503)
        client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            max_retries=1,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        return cls(
            client,
            model=settings.generator_model,
            structured_output=settings.generator_structured_output,
        )

    async def parse(self, prompt: str, slugs: list[str]) -> ParsedPrompt:
        if not slugs:
            raise ParseError("no categories exist yet; create one first", status=422)
        message = await self._call(build_user_prompt(prompt, slugs))
        return normalize(self._json_of(message), slugs)

    async def _call(self, user_prompt: str) -> Message:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if self._structured_output:
            request["output_config"] = {"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}}
        try:
            return await self._client.messages.create(**request)
        except anthropic.APIError as exc:
            log.warning("prompt parse failed: %s: %s", type(exc).__name__, exc)
            raise ParseError(f"model request failed: {type(exc).__name__}") from exc

    @staticmethod
    def _json_of(message: Message) -> dict[str, Any]:
        text = "".join(block.text for block in message.content if block.type == "text")
        try:
            data = json.loads(text) if text else None
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            raise ParseError(f"model returned no usable JSON (stop_reason={message.stop_reason})")
        return data
