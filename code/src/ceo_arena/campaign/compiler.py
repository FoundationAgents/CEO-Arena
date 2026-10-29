"""Compile one credential-bearing experiment spec into a frozen run matrix."""

from __future__ import annotations

import math

from ceo_arena.campaign.models import (
    DEFAULT_CAMPAIGN_RUN_LIMIT,
    HARD_CAMPAIGN_RUN_LIMIT,
    RULE_MODEL_ID,
    CampaignEstimate,
    CampaignLineup,
    CampaignRun,
    FrozenCampaignPlan,
    ModelRoute,
    campaign_lineup_id,
    campaign_run_id,
    campaign_task_key,
)
from ceo_arena.campaign.spec import ExperimentSpec
from ceo_arena.config import default_scenario, load_scenario
from ceo_arena.hashing import canonical_model_hash, provider_endpoint_fingerprint
from ceo_arena.models import AgentBudget, ScenarioConfig
from ceo_arena.protocol import LeaveOneOutDesign


class CampaignCompiler:
    """Own fixed-seat lineups, routes, task identities, and scale estimates."""

    def compile(
        self,
        spec: ExperimentSpec,
        *,
        scenario: ScenarioConfig | None = None,
        budget: AgentBudget | None = None,
        simulation_fingerprints: dict[str, str] | None = None,
    ) -> FrozenCampaignPlan:
        """Return a complete fixed-seat Leave-One-Out plan without credentials."""

        design = LeaveOneOutDesign(model_order=spec.model_ids, rule_model_id=RULE_MODEL_ID)
        lineup_count = design.company_count + 1
        run_count = lineup_count * len(spec.seeds)
        if run_count > HARD_CAMPAIGN_RUN_LIMIT:
            raise ValueError(
                f"Campaign has {run_count:,} Runs, above the hard limit "
                f"of {HARD_CAMPAIGN_RUN_LIMIT:,}"
            )
        if run_count > DEFAULT_CAMPAIGN_RUN_LIMIT and not spec.execution.allow_large_campaign:
            raise ValueError(
                f"Campaign has {run_count:,} Runs; set "
                "execution.allow_large_campaign = true after reviewing the plan size"
            )
        routes = tuple(
            sorted(
                (
                    ModelRoute(
                        model_id=model_id,
                        api_index=api_index,
                        inference=spec.inference[model_id],
                    )
                    for api_index, api in enumerate(spec.api)
                    for model_id in api.models
                ),
                key=lambda route: route.model_id,
            )
        )
        lineups = tuple(
            CampaignLineup(
                lineup_id=campaign_lineup_id(model_ids),
                model_ids=model_ids,
            )
            for model_ids in design.seat_ordered_lineups()
        )
        route_indices = {route.model_id: route.api_index for route in routes}
        runs = tuple(
            CampaignRun(
                task_key=campaign_task_key(spec.campaign_id, lineup.lineup_id, seed),
                run_id=campaign_run_id(spec.campaign_id, lineup.lineup_id, seed),
                campaign_id=spec.campaign_id,
                lineup_id=lineup.lineup_id,
                seed=seed,
                total_days=spec.days,
                company_ids=design.company_ids,
                model_ids=lineup.model_ids,
                api_indices=tuple(route_indices.get(model_id) for model_id in lineup.model_ids),
            )
            for seed in spec.seeds
            for lineup in lineups
        )
        scenario_template = self._scenario(spec, scenario)
        return FrozenCampaignPlan(
            campaign_id=spec.campaign_id,
            provider_endpoint_sha256=provider_endpoint_fingerprint(spec.base_url),
            scenario_template=scenario_template,
            scenario_hash=canonical_model_hash(scenario_template),
            company_count=spec.company_count,
            days=spec.days,
            seeds=spec.seeds,
            model_order=spec.model_ids,
            routes=routes,
            lineups=lineups,
            runs=runs,
            budget=budget or AgentBudget(),
            execution=spec.execution,
            simulation_fingerprints=dict(simulation_fingerprints or {}),
            estimate=self._estimate(
                spec,
                len(lineups),
                scenario_template.days_per_decision_window,
            ),
        )

    @staticmethod
    def _scenario(spec: ExperimentSpec, scenario: ScenarioConfig | None) -> ScenarioConfig:
        selected = scenario
        if selected is None:
            selected = load_scenario(spec.scenario) if spec.scenario else default_scenario()
        return selected.model_copy(update={"seed": 0, "total_days": spec.days})

    @staticmethod
    def _estimate(
        spec: ExperimentSpec,
        lineup_count: int,
        decision_window_days: int,
    ) -> CampaignEstimate:
        candidate_count = len(spec.model_ids)
        appearances_per_identity = candidate_count
        seed_count = len(spec.seeds)
        windows = math.ceil(spec.days / decision_window_days)
        model_trajectories = seed_count * candidate_count * appearances_per_identity
        rule_trajectories = seed_count * appearances_per_identity
        run_count = seed_count * lineup_count
        return CampaignEstimate(
            candidate_model_count=candidate_count,
            company_count=spec.company_count,
            seed_count=seed_count,
            lineup_count=lineup_count,
            run_count=run_count,
            decision_window_days=decision_window_days,
            decision_windows_per_trajectory=windows,
            total_company_trajectories=run_count * spec.company_count,
            model_company_trajectories=model_trajectories,
            rule_company_trajectories=rule_trajectories,
            maximum_model_decisions=model_trajectories * windows,
            maximum_rule_decisions=rule_trajectories * windows,
        )


__all__ = ["CampaignCompiler"]
