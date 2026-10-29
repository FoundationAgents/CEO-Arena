"""Reusable symmetric scenarios for economy contract tests."""

from __future__ import annotations

import pytest

from ceo_arena.models import (
    CapacityTierConfig,
    InnovationMechanismConfig,
    MarketingMechanismConfig,
    OperationsMechanismConfig,
    ProductConfig,
    ResearchMechanismConfig,
    ScenarioConfig,
    SegmentConfig,
)


def build_scenario(
    *,
    seed: int = 7,
    days_per_window: int = 1,
    demand: float = 40.0,
    inventory: int = 120,
    capacity: int = 200,
    abandonment_rate: float = 0.5,
    stockout_switch_probability: float = 0.65,
) -> ScenarioConfig:
    """Build a compact scenario whose parameters keep tests interpretable."""
    return ScenarioConfig(
        seed=seed,
        total_days=30,
        days_per_decision_window=days_per_window,
        initial_cash_cents=10_000_000,
        products=(
            ProductConfig(
                product_id="standard",
                base_quality=1.0,
                unit_cost_cents=400,
                salvage_value_cents=100,
                holding_cost_cents=1,
                procurement_lead_days=2,
                initial_inventory=inventory,
                initial_price_cents=1_000,
            ),
        ),
        segments=(
            SegmentConfig(
                segment_id="mainstream",
                base_daily_demand=demand,
                price_reference_cents=1_000,
                price_weight=2.0,
                quality_weight=1.5,
                service_weight=0.4,
                awareness_weight=0.5,
                loyalty_weight=0.2,
                outside_utility=-1.0,
                macro_sensitivity=0.2,
                seasonal_amplitude=0.0,
                seasonal_period_days=30,
                expected_quality=0.8,
                service_level_target=0.5,
                product_match={"standard": 0.2},
            ),
        ),
        capacity_tiers=(
            CapacityTierConfig(
                tier=0,
                units_per_day=capacity,
                daily_cost_cents=100,
                expansion_cost_cents=0,
                expansion_lead_days=1,
            ),
            CapacityTierConfig(
                tier=1,
                units_per_day=capacity * 2,
                daily_cost_cents=180,
                expansion_cost_cents=20_000,
                expansion_lead_days=2,
            ),
        ),
        marketing=MarketingMechanismConfig(
            attention_power=0.5,
            outside_attention=1.0,
            goodwill_decay=0.10,
            awareness_gain=0.6,
            spend_reference_cents=1_000,
        ),
        innovation=InnovationMechanismConfig(
            daily_depreciation=0.01,
            development_delay_days=2,
            quality_scale=0.25,
            capital_reference_cents=10_000,
            project_cost_cents={1: 5_000, 2: 10_000, 3: 20_000},
            project_median_days={1: 2, 2: 3, 3: 4},
            project_capital_gain_cents={1: 4_000, 2: 10_000, 3: 24_000},
        ),
        operations=OperationsMechanismConfig(
            base_failure_probability=0.10,
            failure_probability_floor=0.01,
            reliability_spend_scale_cents=2_000,
            support_agent_daily_cost_cents=1_000,
            tickets_per_agent_day=10.0,
            base_ticket_rate=0.05,
            failure_ticket_rate=0.5,
            satisfaction_alpha=0.2,
            negative_experience_weight=2.0,
            abandonment_daily_rate=abandonment_rate,
            stockout_switch_probability=stockout_switch_probability,
        ),
        research=ResearchMechanismConfig(
            level_cost_cents={1: 100, 2: 200, 3: 300, 4: 400, 5: 500},
            level_delay_days={1: 1, 2: 1, 3: 1, 4: 1, 5: 1},
            level_signal_std={1: 5.0, 2: 3.0, 3: 2.0, 4: 1.0, 5: 0.25},
            prior_std=10.0,
            weekly_process_std=0.1,
        ),
        macro_volatility=0.0,
        tender_interval_days=7,
        tender_bid_window_days=1,
    )


@pytest.fixture
def scenario() -> ScenarioConfig:
    """Return the default compact scenario."""
    return build_scenario()
