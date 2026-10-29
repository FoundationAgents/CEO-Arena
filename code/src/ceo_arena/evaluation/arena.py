"""Complete fixed-seat Leave-One-Out Arena planning and Track A/B/C analysis."""

from __future__ import annotations

import hashlib
import math
import re
import statistics
from collections import Counter

from pydantic import Field, model_validator

from ceo_arena.hashing import canonical_model_hash
from ceo_arena.models import (
    SCENARIO_VERSION,
    AgentBudget,
    RecoverySummary,
    ScenarioConfig,
    StrictModel,
)
from ceo_arena.protocol import RULE_MODEL_ID, LeaveOneOutDesign, canonical_lineup_id
from ceo_arena.runtime.persistence import AgentAssignment, RunConfig

SAFE_ARENA_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
_SAFE_ARENA_ID = re.compile(SAFE_ARENA_ID_PATTERN)


def freeze_scenario_template(scenario: ScenarioConfig) -> ScenarioConfig:
    """Remove the run seed while preserving every economic parameter."""

    return scenario.model_copy(update={"seed": 0})


def lineup_run_slug(lineup_id: str) -> str:
    """Return a deterministic path-safe name for an untrusted lineup id."""

    return f"lineup-{hashlib.sha256(lineup_id.encode('utf-8')).hexdigest()[:16]}"


def arena_run_id(lineup_id: str, seed: int) -> str:
    """Return the canonical run id used by execution and result validation."""

    return f"{lineup_run_slug(lineup_id)}-seed-{seed}"


arena_lineup_id = canonical_lineup_id


class ModelCandidate(StrictModel):
    """One evaluated model route or the deterministic rule CEO."""

    model_id: str = Field(min_length=1)
    harness_id: str = Field(min_length=1)
    api_index: int | None = Field(default=None, ge=0)


class ArenaLineup(StrictModel):
    """One unordered lineup mapped once onto fixed symmetric company ids."""

    lineup_id: str = Field(min_length=1)
    assignments: tuple[AgentAssignment, ...]

    @model_validator(mode="after")
    def validate_identity(self) -> ArenaLineup:
        if not self.assignments:
            raise ValueError("arena lineup requires at least one assignment")
        company_ids = [item.company_id for item in self.assignments]
        model_ids = [item.model_id for item in self.assignments]
        if any(_SAFE_ARENA_ID.fullmatch(item) is None for item in company_ids):
            raise ValueError("arena company ids must be path-safe identifiers")
        if len(company_ids) != len(set(company_ids)):
            raise ValueError("arena lineup company ids must be unique")
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("arena lineup model ids must be unique")
        expected_id = arena_lineup_id(tuple(model_ids))
        if self.lineup_id != expected_id:
            raise ValueError(f"arena lineup id must equal {expected_id!r}")
        return self

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(item.model_id for item in self.assignments)


class ArenaPlan(StrictModel):
    """Frozen complete Leave-One-Out plan used to fail closed on missing results."""

    scenario_id: str = SCENARIO_VERSION
    scenario_template: ScenarioConfig
    scenario_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    seeds: tuple[int, ...]
    lineups: tuple[ArenaLineup, ...]
    budget: AgentBudget = Field(default_factory=AgentBudget)
    provider_endpoint_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_fingerprints: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_plan(self) -> ArenaPlan:
        if not self.seeds or len(self.seeds) != len(set(self.seeds)):
            raise ValueError("arena seeds must be non-empty and unique")
        lineup_ids = [lineup.lineup_id for lineup in self.lineups]
        if not lineup_ids or len(lineup_ids) != len(set(lineup_ids)):
            raise ValueError("arena lineups must be non-empty and unique")
        if self.scenario_template.seed != 0:
            raise ValueError("arena scenario template seed must be zero")
        if self.scenario_template.scenario_id != self.scenario_id:
            raise ValueError("arena scenario id differs from its frozen template")
        if canonical_model_hash(self.scenario_template) != self.scenario_hash:
            raise ValueError("arena scenario template hash is invalid")
        company_ids = tuple(item.company_id for item in self.lineups[0].assignments)
        if any(
            tuple(item.company_id for item in lineup.assignments) != company_ids
            for lineup in self.lineups
        ):
            raise ValueError("every arena lineup must use the same fixed company positions")
        identities: dict[str, tuple[str, int | None]] = {}
        uses_provider = False
        for lineup in self.lineups:
            for assignment in lineup.assignments:
                identity = (assignment.harness_id, assignment.api_index)
                previous = identities.setdefault(assignment.model_id, identity)
                if previous != identity:
                    raise ValueError(f"model {assignment.model_id!r} changes Harness or API route")
                uses_provider = uses_provider or assignment.harness_id.startswith(
                    "opencode-native-"
                )
        if uses_provider and self.provider_endpoint_sha256 is None:
            raise ValueError("arena provider endpoint identity is missing")
        if RULE_MODEL_ID in self.lineups[0].model_ids:
            raise ValueError("Leave-One-Out plan must place L_-R first")
        design = LeaveOneOutDesign(
            model_order=self.lineups[0].model_ids,
            rule_model_id=RULE_MODEL_ID,
        )
        if company_ids != design.company_ids:
            raise ValueError("arena plan must use canonical fixed company positions")
        design.validate_lineups(tuple(lineup.model_ids for lineup in self.lineups))
        return self

    @property
    def expected_run_count(self) -> int:
        return len(self.seeds) * len(self.lineups)


class ArenaRunResult(StrictModel):
    """Official per-company results for one lineup and world seed."""

    run_id: str = Field(min_length=1)
    lineup_id: str
    seed: int
    assignments: tuple[AgentAssignment, ...]
    official_score_by_company_cents: dict[str, int]
    decision_status_by_company: dict[str, dict[str, int]]
    recovery_by_company: dict[str, RecoverySummary] = Field(default_factory=dict)
    scenario_id: str = SCENARIO_VERSION
    config_hash: str = ""
    artifact_fingerprints: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_scores(self) -> ArenaRunResult:
        company_ids = [item.company_id for item in self.assignments]
        model_ids = [item.model_id for item in self.assignments]
        if len(company_ids) != len(set(company_ids)):
            raise ValueError("arena run company ids must be unique")
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("arena run model ids must be unique")
        if set(self.official_score_by_company_cents) != set(company_ids):
            raise ValueError("official scores must contain every assigned company exactly once")
        if set(self.decision_status_by_company) != set(company_ids):
            raise ValueError("decision status diagnostics must contain every assigned company")
        if self.recovery_by_company and set(self.recovery_by_company) != set(company_ids):
            raise ValueError("recovery diagnostics must contain every assigned company")
        for company_id, recovery in self.recovery_by_company.items():
            counts = self.decision_status_by_company[company_id]
            if (
                recovery.recovered_windows > counts.get("sealed", 0)
                or recovery.recovered_windows > recovery.windows_with_errors
                or recovery.windows_with_errors > sum(counts.values())
                or recovery.infrastructure_timeout_count > counts.get("timeout", 0)
            ):
                raise ValueError("recovery diagnostics disagree with window outcomes")
        allowed_statuses = {
            "sealed",
            "timeout",
            "protocol_error",
            "provider_error",
            "budget_exhausted",
        }
        for counts in self.decision_status_by_company.values():
            if (
                not counts
                or not set(counts) <= allowed_statuses
                or any(count <= 0 for count in counts.values())
            ):
                raise ValueError("decision status diagnostics are invalid")
        return self

    def contains(self, model_id: str) -> bool:
        return any(item.model_id == model_id for item in self.assignments)

    def model_score(self, model_id: str) -> int:
        matches = [item for item in self.assignments if item.model_id == model_id]
        if len(matches) != 1:
            raise ValueError(f"model {model_id!r} is not present exactly once")
        return self.official_score_by_company_cents[matches[0].company_id]


class TrackAScore(StrictModel):
    """Average operating score over every containing lineup and seed."""

    model_id: str
    run_count: int = Field(gt=0)
    mean_official_score_cents: float


class TrackBBackground(StrictModel):
    """One opponent composition aggregated over its world seeds."""

    model_ids: tuple[str, ...]
    seed_scores_cents: tuple[int, ...]
    mean_cents: float

    @model_validator(mode="after")
    def validate_mean(self) -> TrackBBackground:
        if not self.seed_scores_cents:
            raise ValueError("Track B background requires at least one seed score")
        if not math.isclose(self.mean_cents, statistics.fmean(self.seed_scores_cents)):
            raise ValueError("Track B background mean is inconsistent")
        return self


class TrackBDistribution(StrictModel):
    """Market-composition stability separated from seed-level noise."""

    model_id: str
    run_count: int = Field(gt=0)
    background_count: int = Field(gt=0)
    scores_cents: tuple[int, ...]
    mean_cents: float
    between_background_std_cents: float = Field(ge=0.0)
    within_background_std_cents: float = Field(ge=0.0)
    backgrounds: tuple[TrackBBackground, ...]
    minimum_cents: int
    maximum_cents: int

    @model_validator(mode="after")
    def validate_summary(self) -> TrackBDistribution:
        flattened = tuple(
            score for background in self.backgrounds for score in background.seed_scores_cents
        )
        background_means = tuple(background.mean_cents for background in self.backgrounds)
        within_squared_error = tuple(
            (score - background.mean_cents) ** 2
            for background in self.backgrounds
            for score in background.seed_scores_cents
        )
        if self.run_count != len(flattened) or self.scores_cents != flattened:
            raise ValueError("Track B run scores are inconsistent with its backgrounds")
        if self.background_count != len(self.backgrounds):
            raise ValueError("Track B background count is inconsistent")
        if not math.isclose(self.mean_cents, statistics.fmean(flattened)):
            raise ValueError("Track B mean is inconsistent")
        if not math.isclose(
            self.between_background_std_cents,
            statistics.pstdev(background_means),
        ):
            raise ValueError("Track B population background standard deviation is inconsistent")
        if not math.isclose(
            self.within_background_std_cents,
            math.sqrt(statistics.fmean(within_squared_error)),
        ):
            raise ValueError("Track B pooled within-background RMS is inconsistent")
        if self.minimum_cents != min(flattened) or self.maximum_cents != max(flattened):
            raise ValueError("Track B score range is inconsistent")
        return self


class TrackCSeedComparison(StrictModel):
    """One same-seed, same-seat replacement of Rule by the target model."""

    seed: int
    background_model_ids: tuple[str, ...]
    target_company_id: str = Field(min_length=1)
    private_difference_cents: int
    others_difference_cents: int
    system_difference_cents: int
    directed_effects_cents: dict[str, int]

    @model_validator(mode="after")
    def validate_decomposition(self) -> TrackCSeedComparison:
        if tuple(sorted(self.directed_effects_cents)) != self.background_model_ids:
            raise ValueError("directed effects must cover every background model exactly once")
        if sum(self.directed_effects_cents.values()) != self.others_difference_cents:
            raise ValueError("directed effects must sum to the other-company difference")
        if (
            self.private_difference_cents + self.others_difference_cents
            != self.system_difference_cents
        ):
            raise ValueError("private and other-company differences must sum to system difference")
        return self


class TrackCComparison(StrictModel):
    """Private, system, and directed effects of replacing Rule with one model."""

    model_id: str
    baseline_model_id: str
    pair_count: int = Field(gt=0)
    seed_comparisons: tuple[TrackCSeedComparison, ...]
    mean_private_difference_cents: float
    mean_others_difference_cents: float
    mean_system_difference_cents: float
    mean_directed_effects_cents: dict[str, float]
    positive_system_seed_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_summary(self) -> TrackCComparison:
        if self.model_id == self.baseline_model_id:
            raise ValueError("Track C target and baseline must differ")
        if self.pair_count != len(self.seed_comparisons):
            raise ValueError("Track C pair count must equal its seed comparisons")
        seeds = [item.seed for item in self.seed_comparisons]
        if len(seeds) != len(set(seeds)):
            raise ValueError("Track C seed comparisons must use unique seeds")
        effect_keys = tuple(sorted(self.mean_directed_effects_cents))
        if any(
            tuple(sorted(item.directed_effects_cents)) != effect_keys
            for item in self.seed_comparisons
        ):
            raise ValueError("Track C directed-effect models must be identical across seeds")
        expected_private = statistics.fmean(
            item.private_difference_cents for item in self.seed_comparisons
        )
        expected_others = statistics.fmean(
            item.others_difference_cents for item in self.seed_comparisons
        )
        expected_system = statistics.fmean(
            item.system_difference_cents for item in self.seed_comparisons
        )
        expected_directed = {
            model_id: statistics.fmean(
                item.directed_effects_cents[model_id] for item in self.seed_comparisons
            )
            for model_id in effect_keys
        }
        expected_positive = sum(item.system_difference_cents > 0 for item in self.seed_comparisons)
        if not math.isclose(self.mean_private_difference_cents, expected_private):
            raise ValueError("Track C mean private difference is inconsistent")
        if not math.isclose(self.mean_others_difference_cents, expected_others):
            raise ValueError("Track C mean other-company difference is inconsistent")
        if not math.isclose(self.mean_system_difference_cents, expected_system):
            raise ValueError("Track C mean system difference is inconsistent")
        if any(
            not math.isclose(self.mean_directed_effects_cents[key], expected_directed[key])
            for key in effect_keys
        ):
            raise ValueError("Track C mean directed effects are inconsistent")
        if self.positive_system_seed_count != expected_positive:
            raise ValueError("Track C positive-system seed count is inconsistent")
        return self


class ModelHealth(StrictModel):
    """Non-scoring Harness reliability diagnostics for one model."""

    model_id: str
    decision_count: int = Field(gt=0)
    failure_count: int = Field(ge=0)
    failure_rate: float = Field(ge=0.0, le=1.0)
    status_counts: dict[str, int]
    recovery: RecoverySummary = Field(default_factory=RecoverySummary)
    recovery_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class ArenaResultBundle(StrictModel):
    plan: ArenaPlan
    results: tuple[ArenaRunResult, ...]


class ArenaAnalysis(StrictModel):
    track_a: dict[str, TrackAScore]
    track_b: dict[str, TrackBDistribution]
    track_c: dict[str, TrackCComparison]
    health: dict[str, ModelHealth]

    @model_validator(mode="after")
    def validate_track_keys(self) -> ArenaAnalysis:
        candidate_ids = set(self.track_a)
        if not candidate_ids and not self.track_b and not self.track_c and not self.health:
            return self
        if set(self.track_b) != candidate_ids or set(self.health) != candidate_ids:
            raise ValueError("Track A, Track B, and health must cover the same candidates")
        baseline_ids = {comparison.baseline_model_id for comparison in self.track_c.values()}
        if len(baseline_ids) != 1:
            raise ValueError("Track C comparisons must use exactly one Rule baseline")
        baseline_model_id = next(iter(baseline_ids))
        if baseline_model_id not in candidate_ids:
            raise ValueError("Track C Rule baseline must be an evaluated candidate")
        if set(self.track_c) != candidate_ids - {baseline_model_id}:
            raise ValueError("Track C must cover every non-Rule candidate exactly once")
        if any(key != value.model_id for key, value in self.track_a.items()):
            raise ValueError("Track A keys must match their model identities")
        if any(key != value.model_id for key, value in self.track_b.items()):
            raise ValueError("Track B keys must match their model identities")
        if any(key != value.model_id for key, value in self.track_c.items()):
            raise ValueError("Track C keys must match their model identities")
        if any(key != value.model_id for key, value in self.health.items()):
            raise ValueError("health keys must match their model identities")
        return self


class ArenaPlanner:
    """Generate one complete fixed-seat Leave-One-Out design."""

    def __init__(
        self,
        models: tuple[ModelCandidate, ...],
        rule_baseline: ModelCandidate,
        company_ids: tuple[str, ...],
    ) -> None:
        self._models = models
        self._baseline = rule_baseline
        self._company_ids = company_ids
        self._validate()

    @property
    def expected_lineup_count(self) -> int:
        """Return K+1 for C(K+1, K)."""

        return len(self._models) + 1

    def lineups(self) -> tuple[ArenaLineup, ...]:
        """Return L_-R followed by each same-seat Rule substitution."""

        design = self._design()
        candidates = {item.model_id: item for item in (*self._models, self._baseline)}
        lineups: list[ArenaLineup] = []
        for model_ids in design.seat_ordered_lineups():
            assignments = tuple(
                self._assignment(company_id, candidates[model_id])
                for company_id, model_id in zip(
                    self._company_ids,
                    model_ids,
                    strict=True,
                )
            )
            lineups.append(
                ArenaLineup(
                    lineup_id=arena_lineup_id(model_ids),
                    assignments=assignments,
                )
            )
        return tuple(lineups)

    @staticmethod
    def _assignment(company_id: str, candidate: ModelCandidate) -> AgentAssignment:
        return AgentAssignment(
            company_id=company_id,
            model_id=candidate.model_id,
            harness_id=candidate.harness_id,
            api_index=candidate.api_index,
        )

    def _design(self) -> LeaveOneOutDesign:
        return LeaveOneOutDesign(
            model_order=tuple(model.model_id for model in self._models),
            rule_model_id=self._baseline.model_id,
        )

    def plan(
        self,
        seeds: tuple[int, ...],
        *,
        scenario: ScenarioConfig,
        budget: AgentBudget | None = None,
        provider_endpoint_sha256: str | None = None,
        artifact_fingerprints: dict[str, str] | None = None,
    ) -> ArenaPlan:
        """Freeze lineups, seeds, scenario identity, and Harness artifacts."""

        template = freeze_scenario_template(scenario)
        return ArenaPlan(
            scenario_id=template.scenario_id,
            scenario_template=template,
            scenario_hash=canonical_model_hash(template),
            seeds=seeds,
            lineups=self.lineups(),
            budget=budget or AgentBudget(),
            provider_endpoint_sha256=provider_endpoint_sha256,
            artifact_fingerprints=dict(artifact_fingerprints or {}),
        )

    def _validate(self) -> None:
        if not self._company_ids:
            raise ValueError("arena requires at least one company")
        if len(self._company_ids) != len(set(self._company_ids)):
            raise ValueError("company ids must be unique")
        pool = (*self._models, self._baseline)
        model_ids = [item.model_id for item in pool]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("model ids must be unique, including rule baseline")
        if self._baseline.model_id != RULE_MODEL_ID:
            raise ValueError(f"rule baseline model id must equal {RULE_MODEL_ID!r}")
        design = self._design()
        if self._company_ids != design.company_ids:
            raise ValueError("company ids must be the canonical fixed Leave-One-Out seats")


class ArenaAnalyzer:
    """Derive Track A/B/C strictly from official per-company scores."""

    def __init__(
        self,
        results: tuple[ArenaRunResult, ...],
        plan: ArenaPlan | None = None,
    ) -> None:
        if not results:
            raise ValueError("arena analysis requires at least one run result")
        self._results = results
        self._plan = plan
        if plan is not None:
            self._validate_complete_plan(plan)

    def analyze(self, baseline_model_id: str = RULE_MODEL_ID) -> ArenaAnalysis:
        """Build every official Track and health diagnostic behind one interface."""

        model_ids = self._model_ids()
        if baseline_model_id not in model_ids:
            raise ValueError(f"baseline model {baseline_model_id!r} appears in no runs")
        return ArenaAnalysis(
            track_a={model_id: self.track_a(model_id) for model_id in model_ids},
            track_b={model_id: self.track_b(model_id) for model_id in model_ids},
            track_c={
                model_id: self.track_c(model_id, baseline_model_id)
                for model_id in model_ids
                if model_id != baseline_model_id
            },
            health={model_id: self.health(model_id) for model_id in model_ids},
        )

    def track_a(self, model_id: str) -> TrackAScore:
        scores = self._scores_for(model_id)
        return TrackAScore(
            model_id=model_id,
            run_count=len(scores),
            mean_official_score_cents=statistics.fmean(scores),
        )

    def track_b(self, model_id: str) -> TrackBDistribution:
        grouped: dict[tuple[str, ...], list[int]] = {}
        for result in self._results:
            if not result.contains(model_id):
                continue
            background = tuple(
                sorted(
                    assignment.model_id
                    for assignment in result.assignments
                    if assignment.model_id != model_id
                )
            )
            grouped.setdefault(background, []).append(result.model_score(model_id))
        if not grouped:
            raise ValueError(f"model {model_id!r} appears in no runs")
        backgrounds = tuple(
            TrackBBackground(
                model_ids=background,
                seed_scores_cents=tuple(scores),
                mean_cents=statistics.fmean(scores),
            )
            for background, scores in sorted(grouped.items())
        )
        scores = [score for item in backgrounds for score in item.seed_scores_cents]
        background_means = [item.mean_cents for item in backgrounds]
        within_squared_error = [
            (score - item.mean_cents) ** 2
            for item in backgrounds
            for score in item.seed_scores_cents
        ]
        return TrackBDistribution(
            model_id=model_id,
            run_count=len(scores),
            background_count=len(backgrounds),
            scores_cents=tuple(scores),
            mean_cents=statistics.fmean(scores),
            between_background_std_cents=statistics.pstdev(background_means),
            within_background_std_cents=math.sqrt(statistics.fmean(within_squared_error)),
            backgrounds=backgrounds,
            minimum_cents=min(scores),
            maximum_cents=max(scores),
        )

    def track_c(
        self,
        model_id: str,
        baseline_model_id: str = RULE_MODEL_ID,
    ) -> TrackCComparison:
        """Compare L_-R with L_-B by seed and require an exact seat substitution."""

        if model_id == baseline_model_id:
            raise ValueError("Track C target and baseline must differ")

        target_runs: dict[int, ArenaRunResult] = {}
        baseline_runs: dict[int, ArenaRunResult] = {}
        for result in self._results:
            has_target = result.contains(model_id)
            has_baseline = result.contains(baseline_model_id)
            if has_target == has_baseline:
                continue
            destination = target_runs if has_target else baseline_runs
            if result.seed in destination:
                raise ValueError(
                    "Track C requires exactly one exclusive target or baseline lineup "
                    f"per seed; duplicate seed={result.seed}"
                )
            destination[result.seed] = result

        if set(target_runs) != set(baseline_runs):
            missing_target = sorted(set(baseline_runs) - set(target_runs))
            missing_baseline = sorted(set(target_runs) - set(baseline_runs))
            raise ValueError(
                "incomplete Track C pairing: "
                f"missing_target={missing_target}, missing_baseline={missing_baseline}"
            )
        seeds = sorted(target_runs)
        if not seeds:
            raise ValueError("no leave-one-out Track C pairs")
        comparisons = tuple(
            self._track_c_seed_comparison(
                target_runs[seed],
                baseline_runs[seed],
                model_id,
                baseline_model_id,
            )
            for seed in seeds
        )
        directed_model_ids = tuple(sorted(comparisons[0].directed_effects_cents))
        return TrackCComparison(
            model_id=model_id,
            baseline_model_id=baseline_model_id,
            pair_count=len(comparisons),
            seed_comparisons=comparisons,
            mean_private_difference_cents=statistics.fmean(
                item.private_difference_cents for item in comparisons
            ),
            mean_others_difference_cents=statistics.fmean(
                item.others_difference_cents for item in comparisons
            ),
            mean_system_difference_cents=statistics.fmean(
                item.system_difference_cents for item in comparisons
            ),
            mean_directed_effects_cents={
                other_model_id: statistics.fmean(
                    item.directed_effects_cents[other_model_id] for item in comparisons
                )
                for other_model_id in directed_model_ids
            },
            positive_system_seed_count=sum(
                item.system_difference_cents > 0 for item in comparisons
            ),
        )

    @staticmethod
    def _track_c_seed_comparison(
        target_run: ArenaRunResult,
        baseline_run: ArenaRunResult,
        model_id: str,
        baseline_model_id: str,
    ) -> TrackCSeedComparison:
        """Return one audited L_-R versus L_-B replacement comparison."""

        if target_run.seed != baseline_run.seed:
            raise ValueError("Track C comparisons must use the same seed")
        target_seats = {
            assignment.model_id: assignment.company_id for assignment in target_run.assignments
        }
        baseline_seats = {
            assignment.model_id: assignment.company_id for assignment in baseline_run.assignments
        }
        target_background = set(target_seats) - {model_id}
        baseline_background = set(baseline_seats) - {baseline_model_id}
        if target_background != baseline_background:
            raise ValueError(
                "Track C lineups must differ only by target versus baseline model: "
                f"seed={target_run.seed}"
            )
        target_company_id = target_seats[model_id]
        if baseline_seats[baseline_model_id] != target_company_id:
            raise ValueError(
                "Track C target and baseline must occupy the same company seat: "
                f"seed={target_run.seed}"
            )
        moved_models = sorted(
            other_model_id
            for other_model_id in target_background
            if target_seats[other_model_id] != baseline_seats[other_model_id]
        )
        if moved_models:
            raise ValueError(
                "Track C background models must keep the same company seats: "
                f"seed={target_run.seed}, moved={moved_models}"
            )
        background_model_ids = tuple(sorted(target_background))
        directed_effects = {
            other_model_id: target_run.model_score(other_model_id)
            - baseline_run.model_score(other_model_id)
            for other_model_id in background_model_ids
        }
        private_difference = target_run.model_score(model_id) - baseline_run.model_score(
            baseline_model_id
        )
        others_difference = sum(directed_effects.values())
        system_difference = sum(target_run.official_score_by_company_cents.values()) - sum(
            baseline_run.official_score_by_company_cents.values()
        )
        return TrackCSeedComparison(
            seed=target_run.seed,
            background_model_ids=background_model_ids,
            target_company_id=target_company_id,
            private_difference_cents=private_difference,
            others_difference_cents=others_difference,
            system_difference_cents=system_difference,
            directed_effects_cents=directed_effects,
        )

    def health(self, model_id: str) -> ModelHealth:
        counts: Counter[str] = Counter()
        recovery = RecoverySummary()
        for result in self._results:
            matches = [
                assignment for assignment in result.assignments if assignment.model_id == model_id
            ]
            if not matches:
                continue
            company_id = matches[0].company_id
            counts.update(result.decision_status_by_company[company_id])
            recovery.merge(result.recovery_by_company.get(company_id, RecoverySummary()))
        if not counts:
            raise ValueError(f"model {model_id!r} appears in no runs")
        total = sum(counts.values())
        failures = total - counts.get("sealed", 0)
        return ModelHealth(
            model_id=model_id,
            decision_count=total,
            failure_count=failures,
            failure_rate=failures / total,
            status_counts=dict(sorted(counts.items())),
            recovery=recovery,
            recovery_rate=recovery.recovery_rate,
        )

    def _scores_for(self, model_id: str) -> list[int]:
        scores = [
            result.model_score(model_id) for result in self._results if result.contains(model_id)
        ]
        if not scores:
            raise ValueError(f"model {model_id!r} appears in no runs")
        return scores

    def _model_ids(self) -> tuple[str, ...]:
        source = self._plan.lineups if self._plan is not None else self._results
        return tuple(
            sorted({assignment.model_id for item in source for assignment in item.assignments})
        )

    def _validate_complete_plan(self, plan: ArenaPlan) -> None:
        expected = {
            (lineup.lineup_id, seed): lineup for lineup in plan.lineups for seed in plan.seeds
        }
        actual: dict[tuple[str, int], ArenaRunResult] = {}
        for result in self._results:
            key = (result.lineup_id, result.seed)
            if key in actual:
                raise ValueError(f"duplicate arena result: {key}")
            actual[key] = result
        if set(actual) != set(expected):
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            raise ValueError(f"arena result set is incomplete: missing={missing}, extra={extra}")
        for key, result in actual.items():
            lineup = expected[key]
            if result.assignments != lineup.assignments:
                raise ValueError(f"arena assignments drifted for {key}")
            if result.scenario_id != plan.scenario_id:
                raise ValueError(f"arena scenario drifted for {key}")
            if result.artifact_fingerprints != plan.artifact_fingerprints:
                raise ValueError(f"arena Harness artifacts drifted for {key}")
            expected_config = RunConfig(
                run_id=result.run_id,
                scenario=plan.scenario_template.model_copy(update={"seed": result.seed}),
                assignments=lineup.assignments,
                budget=plan.budget,
                provider_endpoint_sha256=plan.provider_endpoint_sha256,
                artifact_fingerprints=plan.artifact_fingerprints,
            )
            if result.config_hash != expected_config.config_hash:
                raise ValueError(f"arena run configuration drifted for {key}")
