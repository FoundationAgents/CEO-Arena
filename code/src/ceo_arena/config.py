"""Scenario configuration loading and the public reference scenario."""

from __future__ import annotations

from pathlib import Path

from ceo_arena.models import (
    CapacityTierConfig,
    InnovationMechanismConfig,
    MarketDynamicsConfig,
    MarketingMechanismConfig,
    MarketPhaseConfig,
    OperationsMechanismConfig,
    ProductConfig,
    ResearchMechanismConfig,
    ScenarioConfig,
    SegmentConfig,
)


def load_scenario(path: Path) -> ScenarioConfig:
    """Load and validate a scenario JSON file."""
    return ScenarioConfig.model_validate_json(path.read_text(encoding="utf-8"))


def default_scenario(*, seed: int = 42, total_days: int = 500) -> ScenarioConfig:
    """Return the symmetric public reference scenario."""
    return ScenarioConfig(
        seed=seed,
        total_days=total_days,
        initial_cash_cents=50_000_000,
        products=(
            ProductConfig(
                product_id="A",
                base_quality=0.55,
                unit_cost_cents=2_600,
                salvage_value_cents=800,
                holding_cost_cents=2,
                procurement_lead_days=5,
                initial_inventory=600,
                initial_price_cents=4_000,
            ),
            ProductConfig(
                product_id="B",
                base_quality=0.68,
                unit_cost_cents=4_800,
                salvage_value_cents=1_500,
                holding_cost_cents=3,
                procurement_lead_days=7,
                initial_inventory=400,
                initial_price_cents=7_500,
            ),
            ProductConfig(
                product_id="C",
                base_quality=0.82,
                unit_cost_cents=8_500,
                salvage_value_cents=2_600,
                holding_cost_cents=6,
                procurement_lead_days=10,
                initial_inventory=200,
                initial_price_cents=13_500,
            ),
        ),
        segments=(
            SegmentConfig(
                segment_id="value",
                base_daily_demand=240,
                price_reference_cents=4_500,
                price_weight=5.8,
                quality_weight=1.7,
                service_weight=0.7,
                awareness_weight=0.3,
                loyalty_weight=0.5,
                outside_utility=-2.145,
                macro_sensitivity=1.2,
                seasonal_amplitude=0.10,
                seasonal_period_days=7,
                expected_quality=0.50,
                service_level_target=2.0,
                product_match={"A": 0.45, "B": 0.05, "C": -0.35},
                channel_effectiveness={"search": 1.25, "display": 0.70, "affiliate": 1.05},
            ),
            SegmentConfig(
                segment_id="mainstream",
                base_daily_demand=300,
                price_reference_cents=8_000,
                price_weight=4.4,
                quality_weight=2.6,
                service_weight=1.0,
                awareness_weight=0.4,
                loyalty_weight=0.8,
                outside_utility=-0.921,
                macro_sensitivity=1.0,
                seasonal_amplitude=0.08,
                seasonal_period_days=30,
                expected_quality=0.63,
                service_level_target=1.5,
                product_match={"A": 0.0, "B": 0.4, "C": 0.0},
                channel_effectiveness={"search": 1.00, "display": 1.10, "affiliate": 0.90},
            ),
            SegmentConfig(
                segment_id="premium",
                base_daily_demand=150,
                price_reference_cents=14_000,
                price_weight=2.2,
                quality_weight=2.7,
                service_weight=1.6,
                awareness_weight=0.3,
                loyalty_weight=1.1,
                outside_utility=0.086,
                macro_sensitivity=0.7,
                seasonal_amplitude=0.12,
                seasonal_period_days=90,
                expected_quality=0.78,
                service_level_target=1.0,
                product_match={"A": -0.45, "B": 0.0, "C": 0.55},
                channel_effectiveness={"search": 0.85, "display": 1.20, "affiliate": 1.15},
            ),
        ),
        capacity_tiers=(
            CapacityTierConfig(
                tier=0,
                units_per_day=60,
                daily_cost_cents=40_000,
                expansion_cost_cents=0,
                expansion_lead_days=1,
            ),
            CapacityTierConfig(
                tier=1,
                units_per_day=90,
                daily_cost_cents=53_000,
                expansion_cost_cents=1_200_000,
                expansion_lead_days=21,
            ),
            CapacityTierConfig(
                tier=2,
                units_per_day=135,
                daily_cost_cents=73_000,
                expansion_cost_cents=3_000_000,
                expansion_lead_days=35,
            ),
            CapacityTierConfig(
                tier=3,
                units_per_day=200,
                daily_cost_cents=100_000,
                expansion_cost_cents=6_000_000,
                expansion_lead_days=49,
            ),
        ),
        marketing=MarketingMechanismConfig(
            attention_power=0.45,
            outside_attention=1.0,
            goodwill_decay=0.025,
            awareness_gain=0.08,
            spend_reference_cents=10_000,
        ),
        innovation=InnovationMechanismConfig(
            daily_depreciation=0.00045,
            development_delay_days=7,
            development_capital_multiplier=2.0,
            quality_scale=0.18,
            capital_reference_cents=5_000_000,
            project_cost_cents={1: 1_000_000, 2: 3_000_000, 3: 7_500_000},
            project_median_days={1: 45, 2: 90, 3: 150},
            project_capital_gain_cents={1: 2_000_000, 2: 7_000_000, 3: 20_000_000},
        ),
        operations=OperationsMechanismConfig(
            base_failure_probability=0.03,
            failure_probability_floor=0.001,
            reliability_spend_scale_cents=6_000,
            support_agent_daily_cost_cents=30_000,
            tickets_per_agent_day=12.0,
            base_ticket_rate=0.015,
            failure_ticket_rate=0.20,
            satisfaction_alpha=0.04,
            negative_experience_weight=1.8,
            abandonment_daily_rate=0.05,
            stockout_switch_probability=0.76,
            remediation_cost_per_failure_cents=1_200,
        ),
        research=ResearchMechanismConfig(
            level_cost_cents={1: 100_000, 2: 250_000, 3: 600_000, 4: 1_250_000, 5: 2_500_000},
            level_delay_days={1: 2, 2: 4, 3: 7, 4: 10, 5: 14},
            level_signal_std={1: 0.65, 2: 0.40, 3: 0.25, 4: 0.15, 5: 0.08},
            prior_std=0.80,
            weekly_process_std=0.03,
        ),
        macro_reversion=0.02,
        macro_volatility=0.0016,
        market_dynamics=MarketDynamicsConfig(
            phases=(
                MarketPhaseConfig(phase_id="balanced"),
                MarketPhaseConfig(
                    phase_id="price_pressure",
                    demand_multiplier=0.90,
                    segment_weights={"value": 1.5, "mainstream": 1.0, "premium": 0.7},
                    price_weight_multiplier=1.08,
                    quality_weight_multiplier=0.90,
                ),
                MarketPhaseConfig(
                    phase_id="quality_growth",
                    demand_multiplier=1.10,
                    segment_weights={"value": 0.8, "mainstream": 1.0, "premium": 1.4},
                    price_weight_multiplier=0.95,
                    quality_weight_multiplier=1.25,
                    quality_expectation_shift=0.035,
                ),
                MarketPhaseConfig(
                    phase_id="delivery_peak",
                    demand_multiplier=1.35,
                    price_weight_multiplier=0.97,
                    service_weight_multiplier=1.35,
                ),
            ),
            transitions=tuple(
                tuple(59 / 60 if i == j else 1 / 180 for j in range(4)) for i in range(4)
            ),
            daily_quality_drift=0.00015,
            maximum_quality_drift=0.075,
        ),
    )


def write_default_scenario(path: Path, *, seed: int = 42, total_days: int = 500) -> None:
    """Write the canonical reference scenario as stable JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        default_scenario(seed=seed, total_days=total_days).model_dump_json(indent=2),
        encoding="utf-8",
    )
