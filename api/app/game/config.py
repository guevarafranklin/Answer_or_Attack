"""GameConfig: every tunable number of the game in one place (spec §1 rule 4,
§2.1). The engine never carries a literal of its own; a session overrides
fields for playtest experiments through `GameConfig.from_overrides`.
"""
from dataclasses import dataclass, fields, replace
from typing import Any, Self


class ConfigError(ValueError):
    """A GameConfig that cannot describe a playable game."""


def _check_choices(name: str, choices: Any) -> None:
    if not isinstance(choices, tuple) or not choices:
        raise ConfigError(f"{name} must be a non-empty tuple of choices, got {choices!r}")
    if any(not isinstance(x, int) or isinstance(x, bool) or x < 0 for x in choices):
        raise ConfigError(f"{name} must be non-negative integers, got {choices!r}")


@dataclass(frozen=True, slots=True)
class GameConfig:
    min_players: int = 2  # 4 for real games; 2 allowed for testing
    max_players: int = 20
    question_count: int = 15
    # Secret starting XP, equal probability among the choices. By default the
    # choices depend on how many players are present at Start (see
    # starting_xp_tiers and docs/SPEC-phase2.md §2.1 balance rationale); an
    # explicit starting_xp_choices wins over the tiers.
    starting_xp_choices: tuple[int, ...] | None = None
    # (max_players, choices) pairs in ascending order of max_players. A game
    # uses the first tier whose max_players covers the present count; larger
    # counts use the last tier. Test games below the smallest tier use it too.
    starting_xp_tiers: tuple[tuple[int, tuple[int, ...]], ...] = (
        (8, (10, 12, 15)),
        (20, (10, 11, 13)),
    )
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
    attack_steal: int = 2  # XP each attacker gains on a failed block (target damage unchanged)
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
        for name in ("grace_ms", "attack_steal"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConfigError(f"{name} must be a non-negative integer, got {value!r}")
        if self.min_players < 2:
            raise ConfigError("min_players must be at least 2")
        if self.max_players < self.min_players:
            raise ConfigError("max_players must be at least min_players")
        if self.starting_xp_choices is not None:
            _check_choices("starting_xp_choices", self.starting_xp_choices)
        if not self.starting_xp_tiers:
            raise ConfigError("starting_xp_tiers must not be empty")
        previous = 0
        for tier in self.starting_xp_tiers:
            if not isinstance(tier, tuple) or len(tier) != 2:
                raise ConfigError(f"starting_xp_tiers entries must be (max_players, choices), got {tier!r}")
            limit, choices = tier
            if not isinstance(limit, int) or isinstance(limit, bool) or limit <= previous:
                raise ConfigError(f"starting_xp_tiers max_players must be increasing integers, got {limit!r}")
            _check_choices("starting_xp_tiers", choices)
            previous = limit
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
        if values.get("starting_xp_choices") is not None:
            values["starting_xp_choices"] = tuple(values["starting_xp_choices"])
        if "starting_xp_tiers" in values:
            values["starting_xp_tiers"] = tuple(
                (limit, tuple(choices)) for limit, choices in values["starting_xp_tiers"]
            )
        return replace(cls(), **values)

    def choices_for(self, player_count: int) -> tuple[int, ...]:
        """Starting XP choices for a game with this many players present at
        Start: the explicit override if set, else the first tier that covers
        the count, else the last tier."""
        if self.starting_xp_choices is not None:
            return self.starting_xp_choices
        for limit, choices in self.starting_xp_tiers:
            if player_count <= limit:
                return choices
        return self.starting_xp_tiers[-1][1]

    def summary(self) -> dict[str, Any]:
        """Plain dict for the `lobby` message and persistence."""
        return {f.name: getattr(self, f.name) for f in fields(self)}
