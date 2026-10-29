import pytest
from factories import observation

from ceo_arena.harness import AgentToolGateway, DecisionDraft
from ceo_arena.harness.draft import DecisionDraftError, DecisionSealedError
from ceo_arena.models import (
    CapacityActionQuote,
    InnovationTierInfo,
    MarketingAllocation,
    MarketResearchRequest,
    ProductDecision,
)


def test_draft_binds_identity_and_sorts_last_writes() -> None:
    draft = DecisionDraft(company_id="acme", world_version=7)
    draft.set_product(ProductDecision(product_id="z", price_cents=100))
    draft.set_product(ProductDecision(product_id="a", reorder_target=3))
    draft.set_product(ProductDecision(product_id="z", promotion_cents=10))
    draft.set_marketing(
        MarketingAllocation(segment_id="value", channel="search", daily_budget_cents=2)
    )
    draft.set_marketing(
        MarketingAllocation(segment_id="value", channel="search", daily_budget_cents=4)
    )

    decision = draft.seal()

    assert decision.company_id == "acme"
    assert decision.world_version == 7
    assert [item.product_id for item in decision.products] == ["a", "z"]
    assert decision.products[1].price_cents == 100
    assert decision.products[1].promotion_cents == 10
    assert decision.marketing[0].daily_budget_cents == 4
    assert draft.seal() is decision


def test_sealed_draft_rejects_mutation() -> None:
    draft = DecisionDraft(company_id="acme", world_version=7)
    draft.seal()

    with pytest.raises(DecisionSealedError):
        draft.set_product(ProductDecision(product_id="basic", price_cents=1_000))


def test_gateway_rejects_identity_free_invalid_product_and_promotion() -> None:
    gateway = AgentToolGateway(observation())

    with pytest.raises(DecisionDraftError, match="unknown product"):
        gateway.set_product(ProductDecision(product_id="secret", price_cents=1))
    gateway.set_product(ProductDecision(product_id="basic", price_cents=100))
    with pytest.raises(DecisionDraftError, match="promotion"):
        gateway.set_product(ProductDecision(product_id="basic", promotion_cents=101))

    staged = AgentToolGateway(observation())
    staged.set_product(ProductDecision(product_id="basic", promotion_cents=90))
    with pytest.raises(DecisionDraftError, match="promotion"):
        staged.set_product(ProductDecision(product_id="basic", price_cents=80))


def test_capacity_draft_has_one_replaceable_action_and_rejects_sealed_edits() -> None:
    draft = DecisionDraft(company_id="acme", world_version=7)
    draft.expand_capacity(2)
    draft.downsize_capacity(0)
    assert draft.preview().capacity_target_tier is None
    assert draft.preview().capacity_downsize_tier == 0
    draft.cancel_capacity_project("project-1")
    assert draft.preview().capacity_downsize_tier is None
    assert draft.seal().cancel_capacity_project_id == "project-1"
    with pytest.raises(DecisionSealedError):
        draft.expand_capacity(3)


def test_gateway_capacity_actions_must_match_authoritative_quotes() -> None:
    visible = observation().model_copy(
        update={
            "capacity_action_quotes": (
                CapacityActionQuote(action="expand_capacity", target_tier=1, charged_cents=1_000),
                CapacityActionQuote(action="downsize_capacity", target_tier=0, charged_cents=500),
                CapacityActionQuote(
                    action="cancel_capacity_project", project_id="project-1", refunded_cents=1234
                ),
            ),
        }
    )
    gateway = AgentToolGateway(visible)
    with pytest.raises(DecisionDraftError, match="not currently available"):
        gateway.expand_capacity(0)
    with pytest.raises(DecisionDraftError, match="not currently available"):
        gateway.cancel_capacity_project("unknown")
    gateway.downsize_capacity(0)
    assert gateway.preview().upfront_charges_cents == 500
    gateway.cancel_capacity_project("project-1")
    assert gateway.preview().upfront_charges_cents == 0
    assert gateway.preview().cash_after_upfront_cents == visible.company.cash_cents + 1234


def test_combined_upfront_validation_leaves_an_unaffordable_draft_editable() -> None:
    visible = observation()
    visible.company.cash_cents = 150_000
    visible = visible.model_copy(
        update={
            "innovation_catalog": (
                InnovationTierInfo(
                    tier=1, cost_cents=40_000, median_days=10, expected_capital_gain_cents=10_000
                ),
                InnovationTierInfo(
                    tier=2, cost_cents=100_000, median_days=10, expected_capital_gain_cents=20_000
                ),
            ),
        }
    )
    gateway = AgentToolGateway(visible)
    gateway.draft.start_research_project(2)
    gateway.request_market_research(MarketResearchRequest(segment_id="value", level=1))
    assert gateway.preview().upfront_charges_cents == 200_000
    with pytest.raises(DecisionDraftError, match="combined upfront"):
        gateway.seal()
    assert not gateway.draft.sealed
    gateway.draft.start_research_project(1)
    assert gateway.seal().research_project_tier == 1


def test_preview_merges_daily_budget_patches_without_counting_procurement() -> None:
    gateway = AgentToolGateway(observation())
    assert gateway.preview().daily_policy_cost_cents == 40_300
    gateway.set_product(
        ProductDecision(product_id="basic", development_budget_cents=0, reorder_target=99999)
    )
    gateway.set_marketing(
        MarketingAllocation(segment_id="value", channel="search", daily_budget_cents=0)
    )
    gateway.draft.set_operations(0, 10)
    assert gateway.preview().daily_policy_cost_cents == 40_010


def test_reset_removes_unsealed_commitments_and_cannot_reopen_a_decision() -> None:
    gateway = AgentToolGateway(observation())
    gateway.draft.start_research_project(1)
    gateway.draft.set_operations(999, 999)
    gateway.draft.reset()
    assert gateway.preview().upfront_charges_cents == 0
    assert gateway.preview().daily_policy_cost_cents == 40_300
    assert gateway.seal().research_project_tier is None
    with pytest.raises(DecisionSealedError):
        gateway.draft.reset()
