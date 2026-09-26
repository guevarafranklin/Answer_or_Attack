"""§9 step 4, validation layer: every §5.2 rejection rule, with stable reason
codes, plus content_hash dedupe against house content and within a batch."""
import copy

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.rules import MAX_OPTION_LEN, MAX_STEM_LEN
from app.services import validation as v
from app.services.validation import CleanItem, content_hash, dedupe, validate_item

MISSING = object()  # "remove this key" marker for item()

GOOD = {
    "difficulty": 3,
    "grade_band": "g7_g9",
    "tags": ["algebra"],
    "correct_index": 2,
    "en": {
        "stem": "What is 2 + 2?",
        "options": ["3", "5", "4", "22"],
        "explanation": "Basic addition.",
    },
    "es": {
        "stem": "¿Cuánto es 2 + 2?",
        "options": ["3", "5", "4", "22"],
        "explanation": "Suma básica.",
    },
}


def item(**changes) -> dict:
    """Deep copy of GOOD with dotted-path overrides: item(**{"en.stem": "x"})."""
    raw = copy.deepcopy(GOOD)
    for path, value in changes.items():
        *parents, leaf = path.split(".")
        target = raw
        for p in parents:
            target = target[p]
        if value is MISSING:
            target.pop(leaf, None)
        else:
            target[leaf] = value
    return raw


def reasons(raw) -> list[str]:
    result = validate_item(raw)
    assert isinstance(result, list), f"expected rejection, got {result!r}"
    return result


# ---------- accepted ----------


def test_good_item_is_clean():
    result = validate_item(GOOD)
    assert isinstance(result, CleanItem)
    assert result.difficulty == 3
    assert result.grade_band == "g7_g9"
    assert result.tags == ["algebra"]
    assert result.correct_index == 2
    assert set(result.translations) == {"en", "es"}
    assert result.translations["es"].stem == "¿Cuánto es 2 + 2?"
    assert result.translations["en"].explanation == "Basic addition."
    assert result.content_hash == content_hash("What is 2 + 2?")


def test_optional_fields_default():
    result = validate_item(item(grade_band=MISSING, tags=MISSING, **{"en.explanation": MISSING}))
    assert isinstance(result, CleanItem)
    assert result.grade_band is None
    assert result.tags == []
    assert result.translations["en"].explanation is None


def test_boundaries_are_inclusive():
    ok = item(
        difficulty=5,
        correct_index=3,
        **{"en.stem": "x" * MAX_STEM_LEN, "es.options": ["a", "b", "c", "x" * MAX_OPTION_LEN]},
    )
    assert isinstance(validate_item(ok), CleanItem)


# ---------- one test per rejection rule ----------


def test_not_an_object():
    assert reasons(["nope"]) == [v.NOT_AN_OBJECT]
    assert reasons(None) == [v.NOT_AN_OBJECT]


@pytest.mark.parametrize("locale", ["en", "es"])
def test_missing_locale(locale):
    assert reasons(item(**{locale: MISSING})) == [f"missing_locale:{locale}"]


def test_locale_not_object():
    assert reasons(item(es="¿Cuánto es 2 + 2?")) == ["locale_not_object:es"]


def test_stem_missing_or_not_string():
    assert reasons(item(**{"en.stem": MISSING})) == ["stem_invalid:en"]
    assert reasons(item(**{"en.stem": 42})) == ["stem_invalid:en"]


def test_stem_empty():
    assert reasons(item(**{"es.stem": "   "})) == ["stem_empty:es"]


def test_stem_too_long():
    assert reasons(item(**{"es.stem": "x" * (MAX_STEM_LEN + 1)})) == ["stem_too_long:es"]


def test_options_not_a_list_of_strings():
    assert reasons(item(**{"en.options": "a,b,c,d"})) == ["options_invalid:en"]
    assert reasons(item(**{"en.options": ["a", "b", "c", 4]})) == ["options_invalid:en"]


@pytest.mark.parametrize("options", [["a", "b", "c"], ["a", "b", "c", "d", "e"]])
def test_options_count(options):
    assert reasons(item(**{"en.options": options})) == ["options_count:en"]


def test_option_empty():
    assert reasons(item(**{"en.options": ["a", "b", "c", " "]})) == ["option_empty:en"]


def test_option_too_long():
    bad = ["a", "b", "c", "x" * (MAX_OPTION_LEN + 1)]
    assert reasons(item(**{"es.options": bad})) == ["option_too_long:es"]


def test_options_duplicate_within_locale():
    assert reasons(item(**{"en.options": ["4", "5", "6", " 4 "]})) == ["options_duplicate:en"]


def test_explanation_not_string():
    assert reasons(item(**{"en.explanation": ["x"]})) == ["explanation_invalid:en"]


@pytest.mark.parametrize(
    "locale, where, value",
    [
        ("en", "stem", "Which of these... All of the above?"),
        ("en", "options", ["1", "2", "3", "None of the above"]),
        ("es", "stem", "¿Cuál? Todas las anteriores"),
        ("es", "options", ["1", "2", "3", "Ninguna de las anteriores"]),
    ],
)
def test_forbidden_phrase(locale, where, value):
    assert reasons(item(**{f"{locale}.{where}": value})) == [f"forbidden_phrase:{locale}"]


@pytest.mark.parametrize("value", [-1, 4, "2", 2.0, True, None, MISSING])
def test_correct_index_invalid(value):
    assert reasons(item(correct_index=value)) == [v.CORRECT_INDEX_INVALID]


@pytest.mark.parametrize("value", [0, 6, "3", 3.0, False, None, MISSING])
def test_difficulty_invalid(value):
    assert reasons(item(difficulty=value)) == [v.DIFFICULTY_INVALID]


@pytest.mark.parametrize("value", ["g13", "adult ", 7])
def test_grade_band_invalid(value):
    assert reasons(item(grade_band=value)) == [v.GRADE_BAND_INVALID]


def test_tags_invalid():
    assert reasons(item(tags="algebra")) == [v.TAGS_INVALID]
    assert reasons(item(tags=["algebra", 1])) == [v.TAGS_INVALID]


def test_all_failing_rules_are_reported_together():
    raw = item(
        difficulty=9,
        correct_index=7,
        es=MISSING,
        **{"en.stem": "x" * 200, "en.options": ["a", "b", "c", "All of the above"]},
    )
    assert reasons(raw) == [
        "stem_too_long:en",
        "forbidden_phrase:en",
        "missing_locale:es",
        v.CORRECT_INDEX_INVALID,
        v.DIFFICULTY_INVALID,
    ]


# ---------- content_hash + dedupe ----------


def test_content_hash_normalizes_case_trim_and_whitespace():
    h = content_hash("What is 2 + 2?")
    assert h == content_hash("  what   IS 2 +\t2? \n")
    assert h != content_hash("What is 2 + 3?")
    assert len(h) == 64


def _clean(stem: str) -> CleanItem:
    result = validate_item(item(**{"en.stem": stem}))
    assert isinstance(result, CleanItem)
    return result


def test_dedupe_rejects_duplicate_within_batch():
    a, b, c = _clean("What is 2 + 2?"), _clean("  WHAT is   2 + 2?"), _clean("What is 3 + 3?")
    seen: set[str] = set()
    out = dedupe([a, b, c], existing=set(), seen=seen)
    assert out == [a, [v.DUPLICATE_IN_BATCH], c]
    assert seen == {a.content_hash, c.content_hash}


def test_dedupe_seen_carries_across_chunks():
    a = _clean("What is 2 + 2?")
    seen: set[str] = set()
    assert dedupe([a], existing=set(), seen=seen) == [a]
    assert dedupe([a], existing=set(), seen=seen) == [[v.DUPLICATE_IN_BATCH]]


def test_dedupe_rejects_existing_house_hash():
    a = _clean("What is 2 + 2?")
    out = dedupe([a], existing={a.content_hash}, seen=set())
    assert out == [[v.DUPLICATE_OF_EXISTING]]


def test_dedupe_reports_both_codes_when_both_apply():
    a = _clean("What is 2 + 2?")
    seen = {a.content_hash}
    out = dedupe([a], existing={a.content_hash}, seen=seen)
    assert out == [[v.DUPLICATE_IN_BATCH, v.DUPLICATE_OF_EXISTING]]


def test_dedupe_seen_only_grows_with_accepted_items():
    """A rejected item (existing collision) must not claim the slot."""
    a = _clean("What is 2 + 2?")
    seen: set[str] = set()
    dedupe([a], existing={a.content_hash}, seen=seen)
    assert seen == set()


# ---------- gate vs diagnostic ----------


def test_malformed_item_does_not_block_a_valid_twin():
    """The gate only remembers accepted hashes: a malformed item with the same
    stem (which never reaches dedupe) must not get the valid twin rejected."""
    malformed = item(difficulty=9)  # same en stem as GOOD
    assert isinstance(validate_item(malformed), list)

    seen: set[str] = set()
    twin = validate_item(item())
    assert isinstance(twin, CleanItem)
    assert dedupe([twin], existing=set(), seen=seen) == [twin]
    assert seen == {twin.content_hash}


def test_repeat_counter_counts_emissions_the_gate_ignored():
    """Diagnostic: malformed emissions still count toward repeat_rate."""
    counter = v.RepeatCounter()
    stream = [
        item(difficulty=9),          # malformed, stem X  -> emitted
        item(),                      # valid, stem X      -> repeat
        item(**{"en.stem": "Y?"}),   # valid, stem Y
        item(es=MISSING),            # malformed, stem X  -> repeat
        item(**{"en.stem": 42}),     # no hashable stem   -> not emitted
        ["not", "an", "object"],     # not hashable       -> not emitted
    ]
    for raw in stream:
        counter.record(v.emitted_hash(raw))
    assert (counter.emitted, counter.repeated) == (4, 2)
    assert counter.repeat_rate == pytest.approx(0.5)


def test_repeat_counter_empty_is_zero():
    assert v.RepeatCounter().repeat_rate == 0.0


def test_emitted_hash_matches_validated_hash():
    raw = item()
    clean = validate_item(raw)
    assert isinstance(clean, CleanItem)
    assert v.emitted_hash(raw) == clean.content_hash
    assert v.emitted_hash(item(**{"en.stem": "  "})) is None
    assert v.emitted_hash(item(en=MISSING)) is None


async def _insert_question(db: AsyncSession, category_id, content_hash: str, pack_id=None):
    await db.execute(
        text(
            "INSERT INTO questions (category_id, difficulty, correct_index, content_hash, pack_id) "
            "VALUES (:c, 3, 0, :h, :p)"
        ),
        {"c": category_id, "h": content_hash, "p": pack_id},
    )


async def _category_and_pack(db: AsyncSession):
    category_id = (
        await db.execute(text("INSERT INTO categories (slug) VALUES ('v-math') RETURNING id"))
    ).scalar_one()
    user_id = (
        await db.execute(text("INSERT INTO users (display_name) VALUES ('u') RETURNING id"))
    ).scalar_one()
    pack_id = (
        await db.execute(
            text("INSERT INTO study_packs (owner_id, title, locale) VALUES (:o, 'p', 'en') RETURNING id"),
            {"o": user_id},
        )
    ).scalar_one()
    return category_id, pack_id


@pytest.mark.asyncio
async def test_existing_house_hashes_finds_house_collision(db: AsyncSession):
    category_id, _ = await _category_and_pack(db)
    a = _clean("What is 2 + 2?")
    await _insert_question(db, category_id, a.content_hash)
    found = await v.existing_house_hashes(db, [a.content_hash, "other"])
    assert found == {a.content_hash}
    assert dedupe([a], existing=found, seen=set()) == [[v.DUPLICATE_OF_EXISTING]]


@pytest.mark.asyncio
async def test_pack_question_with_same_hash_does_not_block_house_insert(db: AsyncSession):
    """The house-hash uniqueness is partial (pack_id IS NULL); so is the dedupe."""
    category_id, pack_id = await _category_and_pack(db)
    a = _clean("What is 2 + 2?")
    await _insert_question(db, category_id, a.content_hash, pack_id=pack_id)

    assert await v.existing_house_hashes(db, [a.content_hash]) == set()
    assert dedupe([a], existing=set(), seen=set()) == [a]
    # And the DB agrees: the house row inserts fine next to the pack row.
    await _insert_question(db, category_id, a.content_hash)
    count = (
        await db.execute(
            text("SELECT count(*) FROM questions WHERE content_hash = :h"), {"h": a.content_hash}
        )
    ).scalar_one()
    assert count == 2


@pytest.mark.asyncio
async def test_existing_house_hashes_empty_input(db: AsyncSession):
    assert await v.existing_house_hashes(db, []) == set()
