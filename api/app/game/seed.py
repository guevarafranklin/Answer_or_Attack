"""One seed per session (sessions.rng_seed), two independent streams.

The draw's option shuffle and the engine's random choices (starting XP,
the board, auto-picks, reserve fallback) both derive from the session's
seed, so a replay needs only the seed, the resolved config and the event
log. They are separate streams — seeded from the seed plus a stream
name — so the number of questions drawn cannot shift the engine's
choices and vice versa. String seeds are hashed with sha512 by
random.Random, independent of PYTHONHASHSEED.
"""
import random
import secrets

SEED_BITS = 63  # fits sessions.rng_seed BIGINT, always non-negative


def new_seed() -> int:
    return secrets.randbits(SEED_BITS)


def stream(seed: int, name: str) -> random.Random:
    return random.Random(f"{seed}:{name}")


def draw_rng(seed: int) -> random.Random:
    """The per-session option shuffle (app.services.sessions.draw_at_start)."""
    return stream(seed, "draw")


def engine_rng(seed: int) -> random.Random:
    """The rng handed to app.game.engine.step for the whole game."""
    return stream(seed, "engine")
