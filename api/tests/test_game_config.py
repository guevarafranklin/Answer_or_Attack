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
        "starting_xp_choices": (10, 18, 30),
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


def test_from_overrides_replaces_only_named_fields():
    cfg = GameConfig.from_overrides({"question_count": 5}, grace_ms=0)
    assert cfg.question_count == 5
    assert cfg.grace_ms == 0
    assert cfg.max_tokens == GameConfig().max_tokens


def test_from_overrides_coerces_choices_to_tuple():
    cfg = GameConfig.from_overrides({"starting_xp_choices": [5, 6]})
    assert cfg.starting_xp_choices == (5, 6)


def test_from_overrides_rejects_unknown_fields():
    with pytest.raises(ConfigError, match="attack_costs"):
        GameConfig.from_overrides({"attack_costs": 2})


def test_from_overrides_validates():
    with pytest.raises(ConfigError):
        GameConfig.from_overrides(max_tokens=0)
