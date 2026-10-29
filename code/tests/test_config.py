from pathlib import Path

import pytest
from pydantic import ValidationError

from ceo_arena.config import default_scenario, load_scenario, write_default_scenario
from ceo_arena.hashing import named_seed, stable_model_hash

REFERENCE_CONFIG = Path(__file__).parents[1] / "configs" / "default.json"
REFERENCE_SCENARIO_HASH = "f5a858f1bd7b0d6cd7e354f40e2db36c4e0df4a6a2299f42ca52dd1511e908fe"


def test_public_scenario_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "scenario.json"
    write_default_scenario(target, seed=7, total_days=28)

    scenario = load_scenario(target)

    assert scenario.seed == 7
    assert scenario.total_days == 28
    assert [product.product_id for product in scenario.products] == ["A", "B", "C"]


def test_scenario_hash_is_stable() -> None:
    first = default_scenario()
    second = default_scenario()

    assert stable_model_hash(first) == stable_model_hash(second)
    assert stable_model_hash(first) == REFERENCE_SCENARIO_HASH
    assert stable_model_hash(first) != stable_model_hash(default_scenario(seed=43))


def test_committed_reference_config_matches_the_code_default() -> None:
    assert load_scenario(REFERENCE_CONFIG) == default_scenario()


def test_named_seed_is_domain_separated() -> None:
    assert named_seed(42, "demand", 1) == named_seed(42, "demand", 1)
    assert named_seed(42, "demand", 1) != named_seed(42, "marketing", 1)


def test_unknown_config_field_fails_loudly() -> None:
    data = default_scenario().model_dump()
    data["unregistered_rule"] = True

    with pytest.raises(ValidationError):
        type(default_scenario()).model_validate(data)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["research"]["level_signal_std"].update({5: 0.9}),
        lambda data: data["innovation"]["project_cost_cents"].update({2: -1}),
        lambda data: data["segments"][0]["channel_effectiveness"].update({"search": -1}),
        lambda data: data["capacity_tiers"][1].update({"units_per_day": 1}),
    ],
)
def test_invalid_mechanism_tables_fail_during_config_validation(mutate) -> None:
    data = default_scenario().model_dump()
    mutate(data)

    with pytest.raises(ValidationError):
        type(default_scenario()).model_validate(data)
