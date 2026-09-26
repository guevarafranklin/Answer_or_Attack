"""Product rules for question text (spec §5.2, §5.3).

Single source of truth for the limits that both the request schemas
(app/schemas/content.py) and the generation validator
(app/services/validation.py) enforce. Each `validate_*` raises RuleViolation
— a ValueError with a stable `code` — and returns the value unchanged on
success. Pydantic surfaces the message; the generation pipeline groups
rejections by the code.
"""
import re
import unicodedata

# A stem longer than 120 chars is unreadable in 10 seconds — a hard product
# constraint, not a style note.
MAX_STEM_LEN = 120
MAX_OPTION_LEN = 60
OPTION_COUNT = 4

# §5.3: never "all/none of the above", in either locale. Matched
# case-insensitively as substrings of the stem and of every option.
FORBIDDEN_PHRASES = (
    "all of the above",
    "none of the above",
    "todas las anteriores",
    "ninguna de las anteriores",
)


class RuleViolation(ValueError):
    """A ValueError that also carries a machine-readable reason code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_stem(stem: str) -> str:
    if not stem.strip():
        raise RuleViolation("stem_empty", "stem must not be empty")
    if len(stem) > MAX_STEM_LEN:
        raise RuleViolation("stem_too_long", f"stem must be at most {MAX_STEM_LEN} characters")
    return stem


def validate_options(options: list[str]) -> list[str]:
    """Exactly OPTION_COUNT options, each within MAX_OPTION_LEN, no duplicates
    (compared case-insensitively after trimming)."""
    if len(options) != OPTION_COUNT:
        raise RuleViolation("options_count", f"exactly {OPTION_COUNT} options required")
    if any(not o.strip() for o in options):
        raise RuleViolation("option_empty", "options must not be empty")
    if any(len(o) > MAX_OPTION_LEN for o in options):
        raise RuleViolation(
            "option_too_long", f"options must be at most {MAX_OPTION_LEN} characters"
        )
    if len({o.strip().lower() for o in options}) != OPTION_COUNT:
        raise RuleViolation("options_duplicate", "options must be distinct")
    return options


def contains_forbidden_phrase(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in FORBIDDEN_PHRASES)


# §5.3 "No answer in the stem". A word of the correct answer counts as
# distinctive when it has letters, is this long, and is not a function word
# / question word that any stem might contain ("Which city..." vs "Mexico
# City"). Numbers are never distinctive: a year or a fraction in the stem is
# how math and comparison questions name their options.
MIN_DISTINCTIVE_WORD_LEN = 4
NON_DISTINCTIVE_WORDS = frozenset(
    {
        # en
        "what", "which", "when", "where", "whom", "whose", "that", "this", "these",
        "those", "from", "with", "into", "than", "then", "their", "there", "about",
        "after", "before", "during", "between", "does", "have", "were", "been",
        "many", "much", "most", "more", "year", "city", "country", "name", "first",
        "largest", "river", "king", "queen", "empire", "battle", "treaty", "century",
        # es
        "cual", "cuales", "cuando", "donde", "como", "cuanto", "cuantos", "cuantas",
        "quien", "quienes", "para", "desde", "entre", "sobre", "hasta", "este", "esta",
        "estos", "estas", "tiene", "tienen", "fueron", "durante", "despues", "antes",
        "ciudad", "pais", "nombre", "primer", "primera", "primero", "mayor", "siglo",
        "reino", "imperio", "batalla", "tratado",
    }
)  # fmt: skip


def _normalize_words(text: str) -> list[str]:
    """Lower-cased, accent-stripped alphanumeric words ("Gengis Kan", "1453")."""
    stripped = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in stripped if not unicodedata.combining(ch))
    return re.findall(r"[0-9a-z]+", stripped.lower())


def _is_distinctive(word: str) -> bool:
    return (
        len(word) >= MIN_DISTINCTIVE_WORD_LEN
        and word not in NON_DISTINCTIVE_WORDS
        and any(ch.isalpha() for ch in word)
    )


def validate_answer_not_in_stem(stem: str, answer: str) -> None:
    """Raise RuleViolation('answer_in_stem') if the stem gives the answer
    away: a distinctive word of the correct answer (see
    MIN_DISTINCTIVE_WORD_LEN / NON_DISTINCTIVE_WORDS) appears in the stem as
    a word, compared case- and accent-insensitively.

    An answer with no distinctive word — a number, a year, a fraction, a
    short word — is never a giveaway: "Which is larger: 3/4 or 2/3?" has to
    name its options in the stem. (A whole-phrase match is implied: if the
    whole answer is in the stem, so is each of its distinctive words.)"""
    distinctive = {w for w in _normalize_words(answer) if _is_distinctive(w)}
    if distinctive & set(_normalize_words(stem)):
        raise RuleViolation("answer_in_stem", "the stem must not contain the correct answer")
