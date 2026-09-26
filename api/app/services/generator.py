"""Question generators (spec §5.2 step 3).

A Generator turns job params into raw items in the §5.2 JSON shape, one
chunk at a time. It returns whatever the backend produced — validation is
the worker's job, not the generator's — and may raise; the worker treats an
exception as that chunk failing and carries on with the rest.

`get_generator()` picks the backend from settings.generator_backend. The
Claude backend is §9 step 5; until then only the stub exists.
"""
import copy
from typing import Any, Protocol

from app.config import settings
from app.rules import MAX_OPTION_LEN, MAX_STEM_LEN
from app.schemas.generation import GenerationParams


class Generator(Protocol):
    #: Stored in generation_jobs.model.
    name: str

    async def generate(
        self, params: GenerationParams, count: int, chunk_index: int
    ) -> list[dict[str, Any]]:
        """Produce `count` raw items for chunk `chunk_index` of the job."""
        ...


def get_generator() -> Generator:
    backend = settings.generator_backend
    if backend == "stub":
        return StubGenerator()
    if backend == "claude":
        raise NotImplementedError("claude generator is §9 step 5")
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


class StubGenerator:
    """Fixed items, no network. Chunk 0 is a deliberately mixed batch that
    fits in one 20-item chunk — one good item, a duplicate of it, and one bad
    item per rejection rule — so a stub job exercises every path in the
    worker. Later chunks are all good and unique.

    Stems are numbered per chunk, so runs are deterministic and dedupe only
    fires where intended.
    """

    name = "stub"

    async def generate(
        self, params: GenerationParams, count: int, chunk_index: int
    ) -> list[dict[str, Any]]:
        if chunk_index != 0:
            start = chunk_index * 1000
            return [_good(start + i, params) for i in range(count)]

        first = _good(0, params)
        mixed: list[Any] = [first, copy.deepcopy(first)]  # second is duplicate_in_batch
        mixed.extend(_bad_items(params))
        mixed.extend(_good(i, params) for i in range(len(mixed), count))
        return mixed[:count]
