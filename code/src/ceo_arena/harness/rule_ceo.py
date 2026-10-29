"""Fixed, single-segment operating playbook; no online strategy optimization."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, NonNegativeInt

from ceo_arena.harness.draft import AgentToolGateway
from ceo_arena.hashing import canonical_model_hash
from ceo_arena.models import (
    AgentUsage,
    CompanyDecision,
    CompanyObservation,
    DecisionWindowRequest,
    DecisionWindowResult,
    MarketingAllocation,
    ProductDecision,
    StrictModel,
)
from ceo_arena.protocol import RULE_MODEL_ID
from ceo_arena.runtime.context import CompanyContext

RULE_POLICY_VERSION = "rule-ceo-fixed-v3"
RULE_PROFILE_PATH = Path(__file__).with_name("rule_profile.json")
PRODUCT_IDS = ("A", "B", "C")
type MarketFocus = Literal["value", "mainstream", "premium"]


class RuleCEOConfig(StrictModel):
    """All frozen playbook parameters; defaults describe the background reference."""

    model_config = ConfigDict(frozen=True)

    focus: MarketFocus = "value"
    price_multiplier_bps: int = Field(default=10_000, gt=0)
    reorder_targets: tuple[NonNegativeInt, NonNegativeInt, NonNegativeInt] = (150, 100, 50)
    development_daily_cents: NonNegativeInt = 5_000
    marketing_daily_cents: NonNegativeInt = 5_000
    reliability_daily_cents: NonNegativeInt = 2_500
    support_daily_cents: NonNegativeInt = 5_000
    cash_floor_cents: NonNegativeInt = 5_000_000

    @property
    def core_product(self) -> str:
        return PRODUCT_IDS[("value", "mainstream", "premium").index(self.focus)]

    @property
    def harness_id(self) -> str:
        return f"{RULE_POLICY_VERSION}-{canonical_model_hash(self)[:12]}"


DEFAULT_RULE_CEO_CONFIG = RuleCEOConfig.model_validate_json(RULE_PROFILE_PATH.read_bytes())
RULE_CEO_HARNESS_ID = DEFAULT_RULE_CEO_CONFIG.harness_id


class RuleCeoHarness:
    """Apply the same fixed playbook at every ordinary decision barrier."""

    def __init__(self, config: RuleCEOConfig | None = None) -> None:
        self._config = config or DEFAULT_RULE_CEO_CONFIG

    @property
    def harness_id(self) -> str:
        return self._config.harness_id

    @property
    def model_id(self) -> str:
        return RULE_MODEL_ID

    def _decision(self, observation: CompanyObservation) -> CompanyDecision:
        company, config = observation.company, self._config
        catalog = {product.product_id: product for product in observation.product_catalog}
        if set(company.prices_cents) != set(PRODUCT_IDS) or not set(PRODUCT_IDS) <= catalog.keys():
            raise ValueError("Fixed Rule requires the visible A/B/C product catalog")
        channels = sorted(company.marketing_budget_cents.get(config.focus, {}))
        if not channels:
            raise ValueError("Fixed Rule requires marketing channels for its target segment")
        optional_spend = company.cash_cents > config.cash_floor_cents
        advertising = config.marketing_daily_cents if optional_spend else 0
        per_channel, remainder = divmod(advertising, len(channels))
        channel_spend = {
            channel: per_channel + int(index < remainder) for index, channel in enumerate(channels)
        }
        gateway = AgentToolGateway(observation)
        for product_id, target in zip(PRODUCT_IDS, config.reorder_targets, strict=True):
            gateway.set_product(
                ProductDecision(
                    product_id=product_id,
                    price_cents=max(
                        1,
                        catalog[product_id].initial_price_cents
                        * config.price_multiplier_bps
                        // 10_000,
                    ),
                    promotion_cents=0,
                    reorder_target=target,
                    development_budget_cents=(
                        config.development_daily_cents
                        if optional_spend and product_id == config.core_product
                        else 0
                    ),
                )
            )
        for segment, cells in sorted(company.marketing_budget_cents.items()):
            for channel in sorted(cells):
                gateway.set_marketing(
                    MarketingAllocation(
                        segment_id=segment,
                        channel=channel,
                        daily_budget_cents=channel_spend[channel] if segment == config.focus else 0,
                    )
                )
        gateway.draft.set_operations(
            reliability_budget_cents=config.reliability_daily_cents,
            support_budget_cents=config.support_daily_cents,
        )
        return gateway.seal()

    async def decide(
        self, request: DecisionWindowRequest, context: CompanyContext
    ) -> DecisionWindowResult:
        """Check scope, then return a sealed decision without inference or forecasting."""
        observation = request.observation
        if (
            context.company_id != request.company_id
            or context.world_version != request.world_version
        ):
            return self._error("company context does not match the decision request")
        if observation.company.company_id != request.company_id:
            return self._error("request company does not match the private observation")
        if observation.world_version != request.world_version:
            return self._error("request world version does not match the private observation")
        try:
            decision = self._decision(observation)
        except ValueError as error:
            return self._error(str(error))
        return DecisionWindowResult(status="sealed", decision=decision, usage=AgentUsage())

    async def cancel(self, company_id: str) -> None:
        """No background inference exists to cancel."""
        del company_id

    async def aclose(self) -> None:
        """No worker resources are owned by this policy."""

    @staticmethod
    def _error(message: str) -> DecisionWindowResult:
        return DecisionWindowResult(status="protocol_error", error=message)
