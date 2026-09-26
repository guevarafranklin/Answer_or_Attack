"""Question generators (spec §5.2 step 3).

A Generator turns job params into raw items in the §5.2 JSON shape, one
chunk at a time. It returns whatever the backend produced — validation is
the worker's job, not the generator's — and may raise; the worker treats an
exception as that chunk failing and carries on with the rest. A backend that
spent money before failing raises `GeneratorError` with the `usage` attached
so the job's cost stays honest.

`get_generator()` picks the backend from settings.generator_backend:
"stub" (fixed items, no network) or "claude" (app.services.claude_generator).
"""
import copy
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import settings
from app.rules import MAX_OPTION_LEN, MAX_STEM_LEN
from app.schemas.generation import GenerationParams
from app.services.validation import CleanItem

TOPIC_SUMMARY_MAX_LEN = 80


@dataclass
class Usage:
    """Token usage and its price for one or more model calls. The backend
    prices its own calls; the worker only sums."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_cents: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cost_cents + other.cost_cents,
        )


@dataclass
class GeneratedChunk:
    #: Raw items in the §5.2 shape — unvalidated, possibly not even dicts.
    items: list[Any]
    usage: Usage = field(default_factory=Usage)


class GeneratorError(Exception):
    """The chunk failed. `usage` is what the failed attempt(s) still cost."""

    def __init__(self, message: str, usage: Usage | None = None):
        super().__init__(message)
        self.usage = usage or Usage()


class Generator(Protocol):
    #: Stored in generation_jobs.model.
    name: str

    async def generate(
        self,
        params: GenerationParams,
        count: int,
        chunk_index: int,
        avoid: Sequence[str] = (),
    ) -> GeneratedChunk:
        """Produce `count` raw items for chunk `chunk_index` of the job.
        `avoid` is the topic summaries (see `topic_summary`) of everything the
        job has accepted so far — a "don't repeat these" hint, not a gate."""
        ...


def topic_summary(item: CleanItem) -> str:
    """Short, model-readable fingerprint of an accepted question for the
    `avoid` hint: its tags and correct answer ("solar system: Jupiter").
    Hashes are opaque and full stems are too long to send 200 of."""
    answer = item.translations["en"].options[item.correct_index]
    summary = f"{', '.join(item.tags)}: {answer}" if item.tags else answer
    return summary[:TOPIC_SUMMARY_MAX_LEN]


def get_generator() -> Generator:
    backend = settings.generator_backend
    if backend == "stub":
        return StubGenerator()
    if backend == "claude":
        from app.services.claude_generator import ClaudeGenerator

        return ClaudeGenerator.from_settings()
    raise ValueError(f"unknown generator_backend: {backend!r}")


# ---------- stub ----------


def _good(i: int, params: GenerationParams) -> dict[str, Any]:
    """A unique, valid item. Difficulty and grade band cycle through the
    job's requested ranges so the stub respects params like a real backend."""
    span = params.difficulty_max - params.difficulty_min + 1
    difficulty = params.difficulty_min + (i % span)
    grade_band = params.grade_bands[i % len(params.grade_bands)] if params.grade_bands else None
    return {
        "difficulty": difficulty,
        "grade_band": grade_band,
        "tags": ["stub"],
        "correct_index": i % 4,
        "en": {
            "stem": f"Stub question {i}: what is {i} + {i}?",
            "options": _options_with_answer(i, str(2 * i)),
            "explanation": f"{i} + {i} = {2 * i}.",
        },
        "es": {
            "stem": f"Pregunta de prueba {i}: ¿cuánto es {i} + {i}?",
            "options": _options_with_answer(i, str(2 * i)),
            "explanation": f"{i} + {i} = {2 * i}.",
        },
    }


def _options_with_answer(i: int, answer: str) -> list[str]:
    distractors = [str(2 * i + d) for d in (1, 2, 3)]
    options = distractors[:]
    options.insert(i % 4, answer)
    return options


_DROP = object()

# (path, value) edits applied to a valid item, one per rejection rule in
# app.services.validation. Paths use "__" to descend into a locale.
_BAD_VARIANTS: list[dict[str, Any]] = [
    {"es": _DROP},                                                      # missing_locale:es
    {"es": "no es un objeto"},                                          # locale_not_object:es
    {"en__stem": _DROP},                                                # stem_invalid:en
    {"es__stem": "   "},                                                # stem_empty:es
    {"en__stem": "x" * (MAX_STEM_LEN + 1)},                             # stem_too_long:en
    {"en__options": "a,b,c,d"},                                         # options_invalid:en
    {"es__options": ["a", "b", "c"]},                                   # options_count:es
    {"en__options": ["a", "b", "c", ""]},                               # option_empty:en
    {"es__options": ["a", "b", "c", "x" * (MAX_OPTION_LEN + 1)]},       # option_too_long:es
    {"en__options": ["a", "b", "c", "A"]},                              # options_duplicate:en
    {"es__explanation": ["not", "a", "string"]},                        # explanation_invalid:es
    {"en__options": ["a", "b", "c", "All of the above"]},               # forbidden_phrase:en
    {"es__stem": "¿Cuál es correcta? Ninguna de las anteriores"},       # forbidden_phrase:es
    {"correct_index": 4},                                               # correct_index_invalid
    {"difficulty": 0},                                                  # difficulty_invalid
    {"grade_band": "g13"},                                              # grade_band_invalid
    {"tags": "not-a-list"},                                             # tags_invalid
]


def _bad_items(params: GenerationParams) -> list[Any]:
    """One item per rejection rule, each built from its own valid base
    (distinct stems, so the only repeat in a stub job is the deliberate one)."""
    items: list[Any] = ["not an object"]  # not_an_object
    for k, changes in enumerate(_BAD_VARIANTS):
        raw = _good(900 + k, params)
        for path, value in changes.items():
            *parents, leaf = path.split("__")
            target = raw
            for p in parents:
                target = target[p]
            if value is _DROP:
                target.pop(leaf, None)
            else:
                target[leaf] = value
        items.append(raw)
    return items


def _mixed_items(params: GenerationParams) -> list[Any]:
    """The stub's opening batch: one good item, a duplicate of it, and one
    bad item per rejection rule."""
    first = _good(0, params)
    return [first, copy.deepcopy(first), *_bad_items(params)]  # second is duplicate_in_batch


#: Length of the stub's mixed opening batch (see StubGenerator).
STUB_MIXED_COUNT = len(_mixed_items(GenerationParams(category_slug="_", count=1)))


class StubGenerator:
    """Fixed items, no network. A stub job is one deterministic stream of
    items: the first STUB_MIXED_COUNT are a deliberately mixed batch — one
    good item, a duplicate of it, and one bad item per rejection rule — so
    the worker exercises every path; everything after that is good and
    unique. Chunk `i` is the stream from `i * chunk size` on, where the
    chunk size is settings.generator_chunk_size (the worker's convention, so
    a short last chunk lands in the right place) or `count` if that is larger.
    """

    name = "stub"

    async def generate(
        self,
        params: GenerationParams,
        count: int,
        chunk_index: int,
        avoid: Sequence[str] = (),
    ) -> GeneratedChunk:
        start = chunk_index * max(settings.generator_chunk_size, count)
        mixed = _mixed_items(params)
        items: list[Any] = mixed[start : start + count]
        items.extend(_good(1000 + i, params) for i in range(max(start, len(mixed)), start + count))
        return GeneratedChunk(items)
