"""app.rules is the single source of the §5.2 text limits; the request
schema must enforce exactly those values."""
import pytest
from pydantic import ValidationError

from app.rules import (
    MAX_OPTION_LEN,
    MAX_STEM_LEN,
    OPTION_COUNT,
    RuleViolation,
    validate_answer_not_in_stem,
    validate_options,
    validate_stem,
)
from app.schemas.content import QuestionTranslationIn

GOOD_OPTIONS = ["a", "b", "c", "d"]


def _translation(**overrides) -> dict:
    return {"stem": "What is 2 + 2?", "options": GOOD_OPTIONS, **overrides}


def test_limits_are_the_spec_values():
    assert (MAX_STEM_LEN, MAX_OPTION_LEN, OPTION_COUNT) == (120, 60, 4)


def test_stem_boundary():
    validate_stem("x" * MAX_STEM_LEN)
    with pytest.raises(ValueError):
        validate_stem("x" * (MAX_STEM_LEN + 1))
    with pytest.raises(ValueError):
        validate_stem("   ")


@pytest.mark.parametrize(
    "options",
    [
        ["a", "b", "c"],
        ["a", "b", "c", "d", "e"],
        ["a", "b", "c", "A "],  # duplicate after trim/casefold
        ["a", "b", "c", "x" * (MAX_OPTION_LEN + 1)],
        ["a", "b", "c", ""],
    ],
)
def test_options_rejected(options):
    with pytest.raises(ValueError):
        validate_options(options)


def test_schema_uses_the_rules():
    QuestionTranslationIn(**_translation(stem="x" * MAX_STEM_LEN))
    with pytest.raises(ValidationError):
        QuestionTranslationIn(**_translation(stem="x" * (MAX_STEM_LEN + 1)))
    with pytest.raises(ValidationError):
        QuestionTranslationIn(**_translation(options=["a", "b", "c"]))
    with pytest.raises(ValidationError):
        QuestionTranslationIn(**_translation(options=["a", "b", "c", "a"]))


# ---------- answer in stem (§5.3) ----------


@pytest.mark.parametrize(
    "stem, answer",
    [
        ("Who painted the Mona Lisa in Florence?", "Leonardo da Vinci"),
        ("In what year did the Byzantine Empire fall?", "1453"),
        ("Which city is the capital of Australia?", "Canberra"),
        ("¿Quién fundó el Imperio mongol?", "Gengis Kan"),
        ("Which planet is the Red Planet?", "Mars"),
        ("Which of these is a star?", "Sun"),
        ("What is 2 + 2?", "4"),
    ],
)
def test_answer_not_in_stem_accepts(stem, answer):
    validate_answer_not_in_stem(stem, answer)


@pytest.mark.parametrize(
    "stem, answer",
    [
        ("Which is larger: 3/4 or 2/3?", "3/4"),
        ("Which is greater: 0.5 or 1/3?", "0.5"),
        ("Which of 9 and 11 is prime?", "11"),
        ("Which star is the Sun?", "Sun"),  # under 4 letters: not distinctive
        ("Which battle ended in 1453?", "1453"),
        ("What is 0 + 0?", "0"),
    ],
)
def test_answer_not_in_stem_ignores_numeric_and_symbolic_answers(stem, answer):
    """Math and comparison stems have to name their options; an answer
    with no distinctive word is never a giveaway, even as a whole phrase."""
    validate_answer_not_in_stem(stem, answer)


@pytest.mark.parametrize(
    "stem, answer",
    [
        ("Who painted the Mona Lisa, Leonardo's masterpiece?", "Leonardo da Vinci"),
        ("Which empire, the Byzantine one, fell in 1453?", "Byzantine Empire"),  # one word
        ("Which city, Canberra, is Australia's capital?", "Canberra"),
        ("Which wonder was in Alexandria?", "Lighthouse of Alexandria"),
        ("¿Qué conquistador, Gengis Kan, unificó Mongolia?", "Gengis Kan"),
        ("¿Quién fue GENGIS KAN?", "Gengis Kan"),  # case-insensitive
        ("¿Qué país tiene Mexico como capital?", "México"),  # accent-insensitive
    ],
)
def test_answer_not_in_stem_rejects(stem, answer):
    with pytest.raises(RuleViolation) as info:
        validate_answer_not_in_stem(stem, answer)
    assert info.value.code == "answer_in_stem"


def test_answer_not_in_stem_ignores_function_and_question_words():
    """Sharing "which"/"city"/"year" with the stem is not a giveaway."""
    validate_answer_not_in_stem("Which city hosted the 2000 Olympics?", "Mexico City")
    validate_answer_not_in_stem("Which river is the longest in Africa?", "Nile River")
    validate_answer_not_in_stem("¿Qué ciudad es la capital de Perú?", "Ciudad de Lima")
    validate_answer_not_in_stem("What is 2 + 2?", "")  # nothing to compare
