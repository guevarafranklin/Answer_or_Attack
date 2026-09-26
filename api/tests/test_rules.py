"""app.rules is the single source of the §5.2 text limits; the request
schema must enforce exactly those values."""
import pytest
from pydantic import ValidationError

from app.rules import MAX_OPTION_LEN, MAX_STEM_LEN, OPTION_COUNT, validate_options, validate_stem
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
