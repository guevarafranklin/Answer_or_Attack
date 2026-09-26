"""The Claude backend for the Generator protocol (spec §5.2 step 3, §5.3).

One Messages API call per chunk asks for both locales at once, as strict
JSON in the §5.2 shape (structured outputs enforce the schema when enabled;
the text is parsed defensively either way). Failure policy, per chunk:

- transient API error (connection/timeout, 429, 5xx): retried once after a
  backoff, then the chunk fails;
- anything else the API rejects (400, 401, ...): the chunk fails at once;
- a response that isn't the expected JSON: the chunk fails, no retry — a
  retry would spend the same tokens for the same prompt.

Every failure is a GeneratorError carrying the usage the attempt(s) still
cost, so the job's cost_cents is right even for chunks that produced
nothing. The worker turns the error into a chunk failure and carries on.

The SDK's own retry loop is disabled (max_retries=0) so "retry once" means
exactly one extra request, visible in tests.
"""
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import anthropic
from anthropic.types import Message

from app.config import settings
from app.models._common import GRADE_BANDS, REGIONS
from app.rules import FORBIDDEN_PHRASES, MAX_OPTION_LEN, MAX_STEM_LEN, OPTION_COUNT
from app.schemas.generation import GenerationParams
from app.services.generator import GeneratedChunk, GeneratorError, Usage

log = logging.getLogger(__name__)

# Explanations are the biggest source of output tokens and only need to be
# short; a prompt-side cap, not a validator rule.
MAX_EXPLANATION_LEN = 200
REQUEST_TIMEOUT_SECONDS = 180.0
RETRY_DELAY_SECONDS = 5.0
RETRY_DELAY_CAP_SECONDS = 60.0
# Never send more hints than this; the newest accepted topics are the ones
# most likely to be repeated in the next chunk.
MAX_AVOID_HINTS = 200

REGION_GUIDANCE = {
    "global": "Avoid facts that only someone from one country would know.",
    "us": "Facts specific to the United States are welcome.",
    "latam": "Facts specific to Latin America are welcome.",
}
assert set(REGION_GUIDANCE) == set(REGIONS)

SYSTEM_PROMPT = f"""You write multiple-choice trivia questions for a fast-paced quiz game. Each question is shown for about ten seconds, so it must be short and instantly readable.

Hard rules — a question that breaks any of these is thrown away:
- Exactly one defensible correct answer. Ambiguity is the main failure mode in trivia; if two options could be argued correct, pick a different question.
- Exactly {OPTION_COUNT} options, all distinct, each at most {MAX_OPTION_LEN} characters.
- Stem at most {MAX_STEM_LEN} characters.
- explanation: one sentence, at most {MAX_EXPLANATION_LEN} characters, in each locale. Say why the answer is right; nothing more.
- Distractors must be plausible and the same category of thing as the answer: all years, all names, all numbers.
- Never use {", ".join(f'"{p}"' for p in FORBIDDEN_PHRASES)} or anything like them.
- No questions whose answer changes over time ("the current president") unless the question's tags include "time_sensitive".
- "es" is a translation of the question, not a transliteration. It must read naturally to both Mexican and Central American Spanish speakers; avoid regionalisms that only one of them uses. Keep the options in the same order in both locales, so correct_index applies to both.
- correct_index is the 0-based position of the correct option. Vary it; do not put the answer in the same position every time.
- tags: 1–3 short lowercase English topic tags, specific to the question ("algebra", "solar system"), used to tell the questions apart.
- grade_band: the school level the question suits ({", ".join(GRADE_BANDS)}). difficulty: 1 (easiest) to 5 (hardest) within that level.

Output only a JSON object of the form
{{"questions": [{{"difficulty": 3, "grade_band": "g7_g9", "tags": ["algebra"], "correct_index": 2,
  "en": {{"stem": "...", "options": ["", "", "", ""], "explanation": "..."}},
  "es": {{"stem": "...", "options": ["", "", "", ""], "explanation": "..."}}}}]}}
with no prose before or after it."""

# JSON schema for structured outputs. Kept to the constructs the API is
# documented to accept (types, enum, required, additionalProperties); the
# numeric and length limits are enforced by the validator afterwards.
_LOCALE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stem": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
        "explanation": {"type": "string"},
    },
    "required": ["stem", "options", "explanation"],
    "additionalProperties": False,
}
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "difficulty": {"type": "integer"},
                    "grade_band": {"type": "string", "enum": list(GRADE_BANDS)},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "correct_index": {"type": "integer"},
                    "en": _LOCALE_SCHEMA,
                    "es": _LOCALE_SCHEMA,
                },
                "required": ["difficulty", "grade_band", "tags", "correct_index", "en", "es"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


def build_user_prompt(params: GenerationParams, count: int, avoid: Sequence[str]) -> str:
    """The per-chunk request: what to write, within which job params, and
    which topics this job already has."""
    category = params.category_slug.replace("-", " ")
    bands = params.grade_bands or list(GRADE_BANDS)
    lines = [
        f"Write exactly {count} questions for the category: {category}.",
        "",
        "Constraints for this batch:",
        f"- difficulty: integers from {params.difficulty_min} to {params.difficulty_max}"
        " inclusive, spread across that range.",
        f"- grade_band: {', '.join(bands)}" + (", spread across them." if len(bands) > 1 else "."),
        f"- region: {params.region}. {REGION_GUIDANCE[params.region]}",
        "- locales: provide both en and es for every question.",
    ]
    if params.style_notes:
        lines.append(f"- style notes from the editor: {params.style_notes}")
    hints = list(dict.fromkeys(avoid))[-MAX_AVOID_HINTS:]  # unique, newest last
    if hints:
        lines += [
            "",
            "This job already has questions on the topics below (tags: answer)."
            " Do not repeat these topics or ask for these answers again:",
            *(f"- {h}" for h in hints),
        ]
    return "\n".join(lines)


def _is_transient(exc: anthropic.APIError) -> bool:
    if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


def _retry_after(exc: anthropic.APIError) -> float | None:
    """Seconds from a Retry-After header, if the error came with one."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    value = response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class ClaudeGenerator:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        model: str,
        price_input_per_mtok: float,
        price_output_per_mtok: float,
        structured_output: bool = True,
        max_tokens: int | None = None,
        retry_delay: float = RETRY_DELAY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._client = client
        self.name = model
        self._price_in = price_input_per_mtok
        self._price_out = price_output_per_mtok
        self._structured_output = structured_output
        self._max_tokens = max_tokens or settings.generator_max_tokens
        self._retry_delay = retry_delay
        self._sleep = sleep

    @classmethod
    def from_settings(cls) -> "ClaudeGenerator":
        if not settings.anthropic_api_key:
            raise ValueError("generator_backend='claude' needs ANTHROPIC_API_KEY")
        client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            max_retries=0,  # retry policy lives here, not in the SDK
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        return cls(
            client,
            model=settings.generator_model,
            price_input_per_mtok=settings.generator_price_input_per_mtok,
            price_output_per_mtok=settings.generator_price_output_per_mtok,
            structured_output=settings.generator_structured_output,
            max_tokens=settings.generator_max_tokens,
        )

    async def generate(
        self,
        params: GenerationParams,
        count: int,
        chunk_index: int,
        avoid: Sequence[str] = (),
    ) -> GeneratedChunk:
        message = await self._call(build_user_prompt(params, count, avoid))
        usage = self._usage_of(message)
        return GeneratedChunk(self._parse(message, usage), usage)

    async def _call(self, user_prompt: str) -> Message:
        request: dict[str, Any] = {
            "model": self.name,
            "max_tokens": self._max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if self._structured_output:
            request["output_config"] = {"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}}

        for attempt in (1, 2):
            try:
                return await self._client.messages.create(**request)
            except anthropic.APIError as exc:
                if attempt == 2 or not _is_transient(exc):
                    raise GeneratorError(f"{type(exc).__name__}: {exc}") from exc
                delay = min(max(self._retry_delay, _retry_after(exc) or 0.0), RETRY_DELAY_CAP_SECONDS)
                log.warning("transient API error (%s); retrying in %.1fs", type(exc).__name__, delay)
                await self._sleep(delay)
        raise AssertionError("unreachable")

    def _usage_of(self, message: Message) -> Usage:
        u = message.usage
        cost_usd = (u.input_tokens * self._price_in + u.output_tokens * self._price_out) / 1_000_000
        return Usage(u.input_tokens, u.output_tokens, cost_usd * 100)

    @staticmethod
    def _parse(message: Message, usage: Usage) -> list[Any]:
        # stop_reason is in every message so a truncation (max_tokens) is
        # identifiable at a glance in stats.chunk_errors.
        stop = f"stop_reason={message.stop_reason}"
        text = "".join(block.text for block in message.content if block.type == "text")
        if not text:
            raise GeneratorError(f"no text in response ({stop})", usage)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise GeneratorError(f"malformed JSON ({stop}): {exc}", usage) from exc
        if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
            raise GeneratorError(f'malformed JSON ({stop}): expected {{"questions": [...]}}', usage)
        return data["questions"]
