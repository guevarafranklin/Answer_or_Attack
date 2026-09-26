"""GameConfig: every tunable number of the game in one place (spec §1 rule 4,
§2.1). The engine never carries a literal of its own; a session overrides
fields for playtest experiments through `GameConfig.from_overrides`.
"""
from dataclasses import dataclass, fields, replace
from typing import Any, Self


class ConfigError(ValueError):
    """A GameConfig that cannot describe a playable game."""


@dataclass(frozen=True, slots=True)
class GameConfig:
    min_players: int = 2  # 4 for real games; 2 allowed for testing
    max_players: int = 20
    question_count: int = 15
    starting_xp_choices: tuple[int, ...] = (10, 18, 30)  # equal probability, secret
    points_correct: int = 3
    points_wrong: int = -1
    points_timeout: int = -1
    pick_seconds: int = 8  # picker chooses a category; auto-pick on timeout
    question_seconds: int = 10
    reveal_seconds: int = 4
    attack_window_seconds: int = 6
    block_seconds: int = 5
    streak_for_token: int = 2  # consecutive correct answers to earn a token
    max_tokens: int = 2
    attack_cost: int = 1  # XP the attacker pays, win or lose
    attack_damage: int = 3  # XP the target loses on a failed block
    max_incoming_attacks: int = 2  # per target per attack window
    grace_ms: int = 400  # late-arrival allowance (§5)
    rejoin_seconds: int = 60
    board_size: int = 4  # categories offered to the picker each round
    # §2.8: fewer than 2 players present for this long ends the session as
    # abandoned. Not in the §2.1 listing, but rule 4 says it lives here.
    abandon_seconds: int = 60

    def __post_init__(self) -> None:
        positive = (
            "min_players",
            "max_players",
            "question_count",
            "pick_seconds",
            "question_seconds",
            "reveal_seconds",
            "attack_window_seconds",
            "block_seconds",
            "streak_for_token",
            "max_tokens",
            "attack_cost",
            "attack_damage",
            "max_incoming_attacks",
            "rejoin_seconds",
            "board_size",
            "abandon_seconds",
        )
        for name in positive:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ConfigError(f"{name} must be a positive integer, got {value!r}")
        if self.grace_ms < 0:
            raise ConfigError("grace_ms must not be negative")
        if self.min_players < 2:
            raise ConfigError("min_players must be at least 2")
        if self.max_players < self.min_players:
            raise ConfigError("max_players must be at least min_players")
        if not self.starting_xp_choices:
            raise ConfigError("starting_xp_choices must not be empty")
        if any(not isinstance(x, int) or x < 0 for x in self.starting_xp_choices):
            raise ConfigError("starting_xp_choices must be non-negative integers")
        if self.points_correct <= 0:
            raise ConfigError("points_correct must be positive")
        if self.points_wrong > 0 or self.points_timeout > 0:
            raise ConfigError("points_wrong and points_timeout must not be positive")

    @classmethod
    def from_overrides(cls, overrides: dict[str, Any] | None = None, /, **kwargs: Any) -> Self:
        """Defaults with the given fields replaced. Unknown keys raise
        ConfigError so a typo in a playtest override is loud, not silent."""
        values = {**(overrides or {}), **kwargs}
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(values) - known)
        if unknown:
            raise ConfigError(f"unknown GameConfig field(s): {', '.join(unknown)}")
        if "starting_xp_choices" in values:
            values["starting_xp_choices"] = tuple(values["starting_xp_choices"])
        return replace(cls(), **values)

    def summary(self) -> dict[str, Any]:
        """Plain dict for the `lobby` message and persistence."""
        return {f.name: getattr(self, f.name) for f in fields(self)}
