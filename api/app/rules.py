"""Product rules for question text (spec §5.2).

Single source of truth for the limits that both the request schemas
(app/schemas/content.py) and the generation validation layer (§9 step 4)
enforce. Each `validate_*` raises ValueError with a message suitable for
surfacing to an admin, and returns the value unchanged on success.
"""

# A stem longer than 120 chars is unreadable in 10 seconds — a hard product
# constraint, not a style note.
MAX_STEM_LEN = 120
MAX_OPTION_LEN = 60
OPTION_COUNT = 4


def validate_stem(stem: str) -> str:
    if not stem.strip():
        raise ValueError("stem must not be empty")
    if len(stem) > MAX_STEM_LEN:
        raise ValueError(f"stem must be at most {MAX_STEM_LEN} characters")
    return stem


def validate_options(options: list[str]) -> list[str]:
    """Exactly OPTION_COUNT options, each within MAX_OPTION_LEN, no duplicates
    (compared case-insensitively after trimming)."""
    if len(options) != OPTION_COUNT:
        raise ValueError(f"exactly {OPTION_COUNT} options required")
    if any(not o.strip() for o in options):
        raise ValueError("options must not be empty")
    if any(len(o) > MAX_OPTION_LEN for o in options):
        raise ValueError(f"options must be at most {MAX_OPTION_LEN} characters")
    if len({o.strip().lower() for o in options}) != OPTION_COUNT:
        raise ValueError("options must be distinct")
    return options
