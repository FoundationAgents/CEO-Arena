"""Secret-free contracts for one compiled multi-model campaign."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from ceo_arena.hashing import canonical_model_hash
from ceo_arena.inference import InferenceSettings
from ceo_arena.models import AgentBudget, ScenarioConfig, StrictModel
from ceo_arena.protocol import RULE_MODEL_ID, LeaveOneOutDesign, canonical_lineup_id

CAMPAIGN_PLAN_VERSION = "ceos-campaign-plan-v6"
DEFAULT_CAMPAIGN_RUN_LIMIT = 500
DEFAULT_MAX_RUN_ATTEMPTS: int | None = None
HARD_CAMPAIGN_RUN_LIMIT = 100_000
MAX_CONCURRENT_RUNS = 30


campaign_lineup_id = canonical_lineup_id


def campaign_run_id(campaign_id: str, lineup_id: str, seed: int) -> str:
    """Return the campaign-scoped runtime identity of one task."""

    payload = json.dumps((campaign_id, lineup_id, seed), separators=(",", ":")).encode("utf-8")
    return f"run-{hashlib.sha256(payload).hexdigest()}"


def campaign_task_key(campaign_id: str, lineup_id: str, seed: int) -> str:
    """Return the campaign-scoped durable identity of one task."""

    payload = json.dumps((campaign_id, lineup_id, seed), separators=(",", ":")).encode("utf-8")
    return f"task-{hashlib.sha256(payload).hexdigest()}"


class CampaignExecutionSpec(StrictModel):
    """Frozen scheduling policy for a campaign."""

    max_concurrent_runs: int = Field(default=MAX_CONCURRENT_RUNS, ge=1, le=MAX_CONCURRENT_RUNS)
    max_inflight_decisions: int | None = Field(default=None, ge=1)
    max_idle_workers: int | None = Field(default=None, ge=0)
    max_run_attempts: int | None = Field(default=DEFAULT_MAX_RUN_ATTEMPTS, ge=1)
    resource_sample_interval_seconds: float = Field(default=10.0, gt=0)
    schedule: Literal["seed_priority", "seed_wave"] = "seed_priority"
    allow_large_campaign: bool = False


class ModelRoute(StrictModel):
    """Secret-free fixed routing from one model to one API entry."""

    model_id: str = Field(min_length=1)
    api_index: int = Field(ge=0)
    inference: InferenceSettings


class CampaignLineup(StrictModel):
    """One model set ordered by its fixed symmetric company seats."""

    lineup_id: str = Field(pattern=r"^lineup-[0-9a-f]{64}$")
    model_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_identity(self) -> CampaignLineup:
        if not self.model_ids:
            raise ValueError("campaign lineup requires at least one model")
        if len(self.model_ids) != len(set(self.model_ids)):
            raise ValueError("campaign lineup model ids must be unique")
        if self.lineup_id != campaign_lineup_id(self.model_ids):
            raise ValueError("campaign lineup id is not canonical")
        return self


class CampaignRun(StrictModel):
    """One durable task produced by a lineup and economic seed."""

    task_key: str = Field(pattern=r"^task-[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^run-[0-9a-f]{64}$")
    campaign_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    lineup_id: str = Field(pattern=r"^lineup-[0-9a-f]{64}$")
    seed: int
    total_days: int = Field(ge=1)
    company_ids: tuple[str, ...]
    model_ids: tuple[str, ...]
    api_indices: tuple[int | None, ...]

    @model_validator(mode="after")
    def validate_identity(self) -> CampaignRun:
        if self.lineup_id != campaign_lineup_id(self.model_ids):
            raise ValueError("campaign task lineup id differs from its model set")
        if self.run_id != campaign_run_id(self.campaign_id, self.lineup_id, self.seed):
            raise ValueError("campaign run id is not canonical")
        if self.task_key != campaign_task_key(self.campaign_id, self.lineup_id, self.seed):
            raise ValueError("campaign task key is not canonical")
        if not self.company_ids or self.company_ids != tuple(
            f"company-{index + 1}" for index in range(len(self.company_ids))
        ):
            raise ValueError("campaign task company ids must use fixed canonical positions")
        if not (len(self.company_ids) == len(self.model_ids) == len(self.api_indices)):
            raise ValueError("campaign task assignments have inconsistent lengths")
        if len(self.model_ids) != len(set(self.model_ids)):
            raise ValueError("campaign task model ids must be unique")
        for model_id, api_index in zip(self.model_ids, self.api_indices, strict=True):
            if api_index is not None and api_index < 0:
                raise ValueError("campaign task API indices cannot be negative")
            if (model_id == RULE_MODEL_ID) != (api_index is None):
                raise ValueError("only the rule CEO may have no API index")
        return self


class CampaignEstimate(StrictModel):
    """Exact task counts and upper bounds before bankruptcy or early termination."""

    candidate_model_count: int = Field(ge=1)
    company_count: int = Field(ge=1)
    seed_count: int = Field(ge=1)
    lineup_count: int = Field(ge=1)
    run_count: int = Field(ge=1)
    decision_window_days: int = Field(ge=1)
    decision_windows_per_trajectory: int = Field(ge=1)
    total_company_trajectories: int = Field(ge=1)
    model_company_trajectories: int = Field(ge=1)
    rule_company_trajectories: int = Field(ge=1)
    maximum_model_decisions: int = Field(ge=1)
    maximum_rule_decisions: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_totals(self) -> CampaignEstimate:
        if self.company_count != self.candidate_model_count:
            raise ValueError("Leave-One-Out company count must equal provider model count")
        expected_lineups = self.candidate_model_count + 1
        if self.lineup_count != expected_lineups:
            raise ValueError("campaign estimate lineup count is inconsistent")
        if self.run_count != self.seed_count * self.lineup_count:
            raise ValueError("campaign estimate run count is inconsistent")
        if self.total_company_trajectories != self.run_count * self.company_count:
            raise ValueError("campaign estimate company trajectory count is inconsistent")
        if (
            self.model_company_trajectories + self.rule_company_trajectories
            != self.total_company_trajectories
        ):
            raise ValueError("campaign estimate trajectory partition is inconsistent")
        if (
            self.maximum_model_decisions
            != self.model_company_trajectories * self.decision_windows_per_trajectory
        ):
            raise ValueError("campaign estimate model decision count is inconsistent")
        if (
            self.maximum_rule_decisions
            != self.rule_company_trajectories * self.decision_windows_per_trajectory
        ):
            raise ValueError("campaign estimate rule decision count is inconsistent")
        return self


class FrozenCampaignPlan(StrictModel):
    """Immutable, credential-free output of campaign compilation."""

    protocol_version: Literal["ceos-campaign-plan-v6"] = CAMPAIGN_PLAN_VERSION
    campaign_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    provider_endpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario_template: ScenarioConfig
    scenario_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    company_count: int = Field(ge=1)
    days: int = Field(ge=1)
    seeds: tuple[int, ...]
    model_order: tuple[str, ...]
    routes: tuple[ModelRoute, ...]
    lineups: tuple[CampaignLineup, ...]
    runs: tuple[CampaignRun, ...]
    budget: AgentBudget = Field(default_factory=AgentBudget)
    execution: CampaignExecutionSpec
    simulation_fingerprints: dict[str, str] = Field(default_factory=dict)
    estimate: CampaignEstimate

    @property
    def plan_hash(self) -> str:
        """Hash every frozen field while remaining independent of API keys."""

        return canonical_model_hash(self)

    @model_validator(mode="after")
    def validate_plan(self) -> FrozenCampaignPlan:
        if not self.seeds or len(self.seeds) != len(set(self.seeds)):
            raise ValueError("campaign seeds must be non-empty and unique")
        if self.scenario_template.seed != 0 or self.scenario_template.total_days != self.days:
            raise ValueError("campaign scenario must freeze seed zero and the selected horizon")
        if self.scenario_hash != canonical_model_hash(self.scenario_template):
            raise ValueError("campaign scenario hash is invalid")
        route_ids = tuple(route.model_id for route in self.routes)
        if not route_ids or len(route_ids) != len(set(route_ids)):
            raise ValueError("campaign routes require unique model ids")
        if route_ids != tuple(sorted(route_ids)):
            raise ValueError("campaign routes must be canonically sorted")
        if RULE_MODEL_ID in route_ids:
            raise ValueError("the rule CEO must not appear in provider routes")
        api_indices = {route.api_index for route in self.routes}
        if api_indices != set(range(max(api_indices) + 1)):
            raise ValueError("campaign API indices must be contiguous from zero")

        if (
            not self.model_order
            or len(self.model_order) != len(set(self.model_order))
            or set(self.model_order) != set(route_ids)
        ):
            raise ValueError("campaign model order must contain every provider route once")
        design = LeaveOneOutDesign(model_order=self.model_order, rule_model_id=RULE_MODEL_ID)
        if self.company_count != design.company_count:
            raise ValueError("campaign company count differs from its Leave-One-Out design")
        actual_model_sets = tuple(lineup.model_ids for lineup in self.lineups)
        design.validate_lineups(actual_model_sets)
        lineup_ids = tuple(lineup.lineup_id for lineup in self.lineups)
        if len(lineup_ids) != len(set(lineup_ids)):
            raise ValueError("campaign plan contains duplicate lineups")
        expected_runs = tuple((lineup, seed) for seed in self.seeds for lineup in self.lineups)
        actual_runs = tuple((run.lineup_id, run.seed) for run in self.runs)
        if actual_runs != tuple((lineup.lineup_id, seed) for lineup, seed in expected_runs):
            raise ValueError("campaign run matrix is incomplete or out of order")
        route_indices = {route.model_id: route.api_index for route in self.routes}
        for run, (lineup, _seed) in zip(self.runs, expected_runs, strict=True):
            expected_indices = tuple(route_indices.get(model_id) for model_id in lineup.model_ids)
            if run.campaign_id != self.campaign_id:
                raise ValueError("campaign task belongs to another campaign")
            if run.total_days != self.days:
                raise ValueError("campaign task horizon differs from its plan")
            if run.company_ids != design.company_ids or run.model_ids != lineup.model_ids:
                raise ValueError("campaign task assignments differ from their lineup")
            if run.api_indices != expected_indices:
                raise ValueError("campaign task API routing differs from frozen routes")
        if self.estimate.run_count != len(self.runs):
            raise ValueError("campaign estimate differs from its run matrix")
        if self.estimate.candidate_model_count != len(self.routes):
            raise ValueError("campaign estimate differs from its model routes")
        return self


__all__ = [
    "CAMPAIGN_PLAN_VERSION",
    "DEFAULT_CAMPAIGN_RUN_LIMIT",
    "DEFAULT_MAX_RUN_ATTEMPTS",
    "HARD_CAMPAIGN_RUN_LIMIT",
    "RULE_MODEL_ID",
    "CampaignEstimate",
    "CampaignExecutionSpec",
    "CampaignLineup",
    "CampaignRun",
    "FrozenCampaignPlan",
    "ModelRoute",
    "campaign_lineup_id",
    "campaign_run_id",
    "campaign_task_key",
]
