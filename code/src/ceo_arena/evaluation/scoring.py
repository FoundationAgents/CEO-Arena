"""Single official efficiency score plus non-scoring diagnostics."""

from __future__ import annotations

from pydantic import Field

from ceo_arena.models import CompanyState, CompanyStatus, ScenarioConfig, StrictModel, WorldState


class CompanyDiagnostics(StrictModel):
    """Interpretability metrics that never enter the official score."""

    status: CompanyStatus
    initial_enterprise_value_cents: int = Field(ge=0)
    terminal_cash_cents: int
    inventory_salvage_cents: int = Field(ge=0)
    cumulative_revenue_cents: int
    cumulative_cost_cents: int
    consumer_orders: int = Field(ge=0)
    fulfilled_units: int = Field(ge=0)
    abandoned_units: int = Field(ge=0)


class CompanyScore(StrictModel):
    """The sole official score accompanied by explicitly separate diagnostics."""

    company_id: str
    official_score_cents: int
    diagnostics: CompanyDiagnostics


def official_score(company: CompanyState, scenario: ScenarioConfig) -> CompanyScore:
    """Compute terminal cash plus inventory salvage minus initial enterprise value."""

    salvage_by_product = {
        product.product_id: product.salvage_value_cents for product in scenario.products
    }
    unknown_products = (
        set(company.inventory) | {shipment.product_id for shipment in company.inbound_shipments}
    ) - set(salvage_by_product)
    if unknown_products:
        raise ValueError(f"inventory contains unconfigured products: {sorted(unknown_products)}")
    on_hand_salvage = sum(
        quantity * salvage_by_product[product_id]
        for product_id, quantity in company.inventory.items()
    )
    inbound_salvage = sum(
        shipment.quantity * salvage_by_product[shipment.product_id]
        for shipment in company.inbound_shipments
    )
    salvage_cents = on_hand_salvage + inbound_salvage
    initial_value = company.initial_enterprise_value_cents
    score_cents = company.cash_cents + salvage_cents - initial_value
    return CompanyScore(
        company_id=company.company_id,
        official_score_cents=score_cents,
        diagnostics=CompanyDiagnostics(
            status=company.status,
            initial_enterprise_value_cents=initial_value,
            terminal_cash_cents=company.cash_cents,
            inventory_salvage_cents=salvage_cents,
            cumulative_revenue_cents=company.cumulative_revenue_cents,
            cumulative_cost_cents=company.cumulative_cost_cents,
            consumer_orders=company.cumulative_consumer_orders,
            fulfilled_units=company.cumulative_fulfilled_units,
            abandoned_units=company.cumulative_abandoned_units,
        ),
    )


def score_world(state: WorldState, scenario: ScenarioConfig) -> dict[str, CompanyScore]:
    """Score every company independently with the sole official metric."""

    return {
        company_id: official_score(company, scenario)
        for company_id, company in sorted(state.companies.items())
    }
