"""Validation and dedupe for generated questions (spec §5.2 step 4, §3.1).

`validate_item` is pure: it takes one raw item in the §5.2 JSON shape and
returns either a `CleanItem` ready to insert or a list of rejection reason
codes. Codes are stable strings so job stats can be grouped by reason;
per-locale codes carry a `:<locale>` suffix (`stem_too_long:es`).

Text limits come from app.rules — nothing is redefined here.

Dedupe is split so the validator stays pure: `existing_house_hashes` is the
one DB query, `dedupe` is a pure pass over already-validated items.

Two distinct things track hashes per job:
- the gate (`dedupe` + its `seen` set of *accepted* hashes) decides what
  gets inserted;
- the diagnostic (`RepeatCounter` fed by `emitted_hash`) counts every
  emission, including malformed ones, and yields `repeat_rate`.
"""
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Question
from app.models._common import GRADE_BANDS, LOCALES
from app.rules import (
    OPTION_COUNT,
    RuleViolation,
    contains_forbidden_phrase,
    validate_answer_not_in_stem,
    validate_options,
    validate_stem,
)

MIN_DIFFICULTY, MAX_DIFFICULTY = 1, 5

# Reason codes. Listed so tests and the admin UI can enumerate them.
NOT_AN_OBJECT = "not_an_object"
MISSING_LOCALE = "missing_locale"  # :locale
LOCALE_NOT_OBJECT = "locale_not_object"  # :locale
STEM_INVALID = "stem_invalid"  # :locale  (missing or not a string)
OPTIONS_INVALID = "options_invalid"  # :locale  (not a list of strings)
EXPLANATION_INVALID = "explanation_invalid"  # :locale
FORBIDDEN_PHRASE = "forbidden_phrase"  # :locale
ANSWER_IN_STEM = "answer_in_stem"  # :locale  (§5.3: the stem gives the answer away)
CORRECT_INDEX_INVALID = "correct_index_invalid"
DIFFICULTY_INVALID = "difficulty_invalid"
GRADE_BAND_INVALID = "grade_band_invalid"
TAGS_INVALID = "tags_invalid"
DUPLICATE_IN_BATCH = "duplicate_in_batch"
DUPLICATE_OF_EXISTING = "duplicate_of_existing"


@dataclass(frozen=True)
class CleanTranslation:
    stem: str
    options: list[str]
    explanation: str | None


@dataclass(frozen=True)
class CleanItem:
    difficulty: int
    grade_band: str | None
    tags: list[str]
    correct_index: int
    translations: dict[str, CleanTranslation]  # keyed by locale, all of LOCALES present
    content_hash: str


Rejections = list[str]


def content_hash(en_stem: str) -> str:
    """§3.1: sha256 of the en stem, lower-cased, trimmed, internal whitespace
    collapsed to single spaces."""
    normalized = re.sub(r"\s+", " ", en_stem.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_translation(locale: str, raw: Any, reasons: Rejections) -> CleanTranslation | None:
    if not isinstance(raw, dict):
        reasons.append(f"{LOCALE_NOT_OBJECT}:{locale}")
        return None

    stem = raw.get("stem")
    if not isinstance(stem, str):
        reasons.append(f"{STEM_INVALID}:{locale}")
        stem = None
    else:
        try:
            validate_stem(stem)
        except RuleViolation as exc:
            reasons.append(f"{exc.code}:{locale}")

    options = raw.get("options")
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        reasons.append(f"{OPTIONS_INVALID}:{locale}")
        options = None
    else:
        try:
            validate_options(options)
        except RuleViolation as exc:
            reasons.append(f"{exc.code}:{locale}")

    explanation = raw.get("explanation")
    if explanation is not None and not isinstance(explanation, str):
        reasons.append(f"{EXPLANATION_INVALID}:{locale}")
        explanation = None

    texts = [stem] if stem is not None else []
    texts += options or []
    if any(contains_forbidden_phrase(t) for t in texts):
        reasons.append(f"{FORBIDDEN_PHRASE}:{locale}")

    if stem is None or options is None:
        return None
    return CleanTranslation(stem=stem, options=options, explanation=explanation)


def validate_item(raw: Any) -> CleanItem | Rejections:
    """Validate one generated item. Returns a CleanItem, or a non-empty list
    of reason codes. Every failing rule is reported, not just the first, so
    a bad prompt shows all of its symptoms in the job stats."""
    if not isinstance(raw, dict):
        return [NOT_AN_OBJECT]

    reasons: Rejections = []
    translations: dict[str, CleanTranslation] = {}
    for locale in LOCALES:
        if locale not in raw:
            reasons.append(f"{MISSING_LOCALE}:{locale}")
            continue
        clean = _validate_translation(locale, raw[locale], reasons)
        if clean is not None:
            translations[locale] = clean

    correct_index = raw.get("correct_index")
    if not _is_int(correct_index) or not 0 <= correct_index < OPTION_COUNT:
        reasons.append(CORRECT_INDEX_INVALID)
    else:
        for locale, clean in translations.items():
            if correct_index < len(clean.options):
                try:
                    validate_answer_not_in_stem(clean.stem, clean.options[correct_index])
                except RuleViolation as exc:
                    reasons.append(f"{exc.code}:{locale}")

    difficulty = raw.get("difficulty")
    if not _is_int(difficulty) or not MIN_DIFFICULTY <= difficulty <= MAX_DIFFICULTY:
        reasons.append(DIFFICULTY_INVALID)

    grade_band = raw.get("grade_band")
    if grade_band is not None and grade_band not in GRADE_BANDS:
        reasons.append(GRADE_BAND_INVALID)

    tags = raw.get("tags", [])
    if tags is None:
        tags = []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        reasons.append(TAGS_INVALID)

    if reasons:
        return reasons
    return CleanItem(
        difficulty=difficulty,
        grade_band=grade_band,
        tags=list(tags),
        correct_index=correct_index,
        translations=translations,
        content_hash=content_hash(translations["en"].stem),
    )


async def existing_house_hashes(db: AsyncSession, hashes: Iterable[str]) -> set[str]:
    """Which of `hashes` already exist as house content (`pack_id IS NULL`).
    Pack content with the same hash does not count — the partial unique
    index only covers the house pool, and so does this check."""
    wanted = set(hashes)
    if not wanted:
        return set()
    result = await db.execute(
        select(Question.content_hash).where(
            Question.pack_id.is_(None), Question.content_hash.in_(wanted)
        )
    )
    return set(result.scalars())


def dedupe(
    items: Iterable[CleanItem], existing: set[str], seen: set[str]
) -> list[CleanItem | Rejections]:
    """The gate. Reject items whose hash is already in `existing` (house
    content) or in `seen` (accepted earlier in this job); both codes are
    reported when both apply. `seen` holds *accepted* hashes only and is
    updated in place so a caller can carry it across chunks — a malformed
    item never poisons the slot for a valid twin that comes later."""
    out: list[CleanItem | Rejections] = []
    for item in items:
        reasons: Rejections = []
        if item.content_hash in seen:
            reasons.append(DUPLICATE_IN_BATCH)
        if item.content_hash in existing:
            reasons.append(DUPLICATE_OF_EXISTING)
        if reasons:
            out.append(reasons)
        else:
            seen.add(item.content_hash)
            out.append(item)
    return out


def emitted_hash(raw: Any) -> str | None:
    """The hash an item *would* have, from its en stem alone, or None when
    there is no string stem to hash. Computed before validation so the
    diagnostic counter sees every emission, well-formed or not."""
    if not isinstance(raw, dict) or not isinstance(raw.get("en"), dict):
        return None
    stem = raw["en"].get("stem")
    if not isinstance(stem, str) or not stem.strip():
        return None
    return content_hash(stem)


@dataclass
class RepeatCounter:
    """The diagnostic, separate from the gate: how often the generator
    repeats itself across a whole job, regardless of whether the repeated
    items were accepted, rejected, or malformed.

    repeat_rate = repeated emissions ÷ total emitted (hashable) items.
    """

    emitted: int = 0
    repeated: int = 0
    _hashes: set[str] = dataclass_field(default_factory=set, repr=False)

    def record(self, hash_: str | None) -> None:
        if hash_ is None:
            return
        self.emitted += 1
        if hash_ in self._hashes:
            self.repeated += 1
        else:
            self._hashes.add(hash_)

    @property
    def repeat_rate(self) -> float:
        return self.repeated / self.emitted if self.emitted else 0.0
