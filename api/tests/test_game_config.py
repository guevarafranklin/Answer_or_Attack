"""GameConfig (spec §2.1): the defaults are the spec's numbers, every
knob is validated, and session overrides cannot misspell a field."""
import dataclasses

import pytest

from app.game.config import ConfigError, GameConfig


def test_defaults_are_the_spec_values():
    cfg = GameConfig()
    assert cfg.summary() == {
        "min_players": 2,
        "max_players": 20,
        "question_count": 15,
        "starting_xp_choices": None,
        "starting_xp_tiers": ((8, (10, 12, 15)), (20, (10, 11, 13))),
        "points_correct": 3,
        "points_wrong": -1,
        "points_timeout": -1,
        "pick_seconds": 8,
        "question_seconds": 10,
        "reveal_seconds": 4,
        "attack_window_seconds": 6,
        "block_seconds": 5,
        "streak_for_token": 2,
        "max_tokens": 2,
        "attack_cost": 1,
        "attack_damage": 3,
        "attack_steal": 2,
        "max_incoming_attacks": 2,
        "grace_ms": 400,
        "rejoin_seconds": 60,
        "board_size": 4,
        "abandon_seconds": 60,
    }


def test_is_frozen():
    cfg = GameConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.max_tokens = 5  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"min_players": 1},
        {"min_players": 0},
        {"max_players": 1},
        {"min_players": 5, "max_players": 4},
        {"question_count": 0},
        {"starting_xp_choices": ()},
        {"starting_xp_choices": (10, -1)},
        {"starting_xp_choices": (10, 1.5)},
        {"starting_xp_choices": (10, True)},
        {"starting_xp_choices": [10, 12]},
        {"starting_xp_tiers": ()},
        {"starting_xp_tiers": ((8, ()),)},
        {"starting_xp_tiers": ((8, (10, -1)),)},
        {"starting_xp_tiers": ((8, (10,)), (8, (12,)))},
        {"starting_xp_tiers": ((20, (10,)), (8, (12,)))},
        {"starting_xp_tiers": ((0, (10,)),)},
        {"starting_xp_tiers": ((8.0, (10,)),)},
        {"starting_xp_tiers": ((8, (10,), "extra"),)},
        {"points_correct": 0},
        {"points_wrong": 1},
        {"points_timeout": 1},
        {"pick_seconds": 0},
        {"question_seconds": -1},
        {"reveal_seconds": 0},
        {"attack_window_seconds": 0},
        {"block_seconds": 0},
        {"streak_for_token": 0},
        {"max_tokens": 0},
        {"attack_cost": 0},
        {"attack_damage": 0},
        {"max_incoming_attacks": 0},
        {"grace_ms": -1},
        {"attack_steal": -1},
        {"attack_steal": 1.0},
        {"rejoin_seconds": 0},
        {"board_size": 0},
        {"abandon_seconds": 0},
        {"board_size": True},
        {"max_tokens": 2.0},
    ],
)
def test_rejects_unplayable_values(overrides):
    with pytest.raises(ConfigError):
        GameConfig(**overrides)


def test_boundary_values_accepted():
    GameConfig(min_players=2, max_players=2, grace_ms=0, points_wrong=0, points_timeout=0)
    GameConfig(starting_xp_choices=(0,))
    GameConfig(attack_steal=0)


def test_from_overrides_replaces_only_named_fields():
    cfg = GameConfig.from_overrides({"question_count": 5}, grace_ms=0)
    assert cfg.question_count == 5
    assert cfg.grace_ms == 0
    assert cfg.max_tokens == GameConfig().max_tokens


def test_from_overrides_coerces_choices_to_tuple():
    cfg = GameConfig.from_overrides({"starting_xp_choices": [5, 6]})
    assert cfg.starting_xp_choices == (5, 6)


def test_from_overrides_coerces_tiers_from_json_lists():
    cfg = GameConfig.from_overrides({"starting_xp_tiers": [[4, [1, 2]], [9, [3]]]})
    assert cfg.starting_xp_tiers == ((4, (1, 2)), (9, (3,)))


def test_from_overrides_accepts_explicit_none_choices():
    assert GameConfig.from_overrides({"starting_xp_choices": None}).starting_xp_choices is None


# ----- choices_for: the spread scales with the table (§2.1 balance rationale) -----


@pytest.mark.parametrize(
    ("players", "expected"),
    [
        (2, (10, 12, 15)),  # test games use the smallest tier
        (3, (10, 12, 15)),
        (4, (10, 12, 15)),
        (8, (10, 12, 15)),
        (9, (10, 11, 13)),
        (20, (10, 11, 13)),
    ],
)
def test_default_tiers_by_player_count(players, expected):
    assert GameConfig().choices_for(players) == expected


def test_counts_beyond_the_last_tier_use_the_last_tier():
    cfg = GameConfig(starting_xp_tiers=((4, (1,)), (6, (2,))))
    assert cfg.choices_for(7) == (2,)
    assert cfg.choices_for(100) == (2,)


def test_explicit_choices_win_over_tiers():
    cfg = GameConfig(starting_xp_choices=(7, 8))
    assert cfg.choices_for(2) == (7, 8)
    assert cfg.choices_for(20) == (7, 8)


def test_tier_boundaries_are_inclusive():
    cfg = GameConfig(starting_xp_tiers=((5, (1,)), (10, (2,))))
    assert cfg.choices_for(5) == (1,)
    assert cfg.choices_for(6) == (2,)


def test_from_overrides_rejects_unknown_fields():
    with pytest.raises(ConfigError, match="attack_costs"):
        GameConfig.from_overrides({"attack_costs": 2})


def test_from_overrides_validates():
    with pytest.raises(ConfigError):
        GameConfig.from_overrides(max_tokens=0)
