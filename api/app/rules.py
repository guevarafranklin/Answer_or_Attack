"""Product rules for question text (spec §5.2, §5.3).

Single source of truth for the limits that both the request schemas
(app/schemas/content.py) and the generation validator
(app/services/validation.py) enforce. Each `validate_*` raises RuleViolation
— a ValueError with a stable `code` — and returns the value unchanged on
success. Pydantic surfaces the message; the generation pipeline groups
rejections by the code.
"""

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
