"""Deterministic capacity adjustments, completion, and unspent-capital refunds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ceo_arena.hashing import semantic_digest
from ceo_arena.models import (
    ActionReceipt,
    CapacityActionQuote,
    CapacityChangeKind,
    CapacityProject,
    CompanyDecision,
    CompanyState,
    CompanyStatus,
    JobStatus,
    ScenarioConfig,
)


@dataclass(frozen=True, slots=True)
class CapacityCashFlow:
    """A capacity action's cash movements, recorded by the owning world."""

    charged_cents: int = 0
    refunded_cents: int = 0
    receipts: tuple[ActionReceipt, ...] = ()
    kind: CapacityChangeKind | None = None


class CapacityManager:
    """Keep one capacity adjustment pending; cash and its ledger belong to the world.

    Expansion costs are cumulative installed-capital prices, so moving from tier
    i to tier j charges the difference. Downsizing never returns installed
    capital. Cancelling unfinished expansion returns only unspent prepaid funds,
    less a cancellation fee. Invalid actions raise before changing any state.
    """

    def __init__(self, scenario: ScenarioConfig) -> None:
        self._seed = scenario.seed
        self._policy = scenario.capacity_policy
        self._tiers = {tier.tier: tier for tier in scenario.capacity_tiers}

    def quotes(self, company: CompanyState, day: int) -> tuple[CapacityActionQuote, ...]:
        """Quote legal requests through the same implementation without changing company state."""
        if company.status is not CompanyStatus.ACTIVE:
            return ()
        requests = [
            request
            for tier in sorted(self._tiers)
            for request in (
                CompanyDecision(
                    company_id=company.company_id, world_version=0, capacity_target_tier=tier
                ),
                CompanyDecision(
                    company_id=company.company_id, world_version=0, capacity_downsize_tier=tier
                ),
            )
        ]
        requests.extend(
            CompanyDecision(
                company_id=company.company_id,
                world_version=0,
                cancel_capacity_project_id=project.project_id,
            )
            for project in company.capacity_projects
            if project.kind is CapacityChangeKind.EXPAND
            and project.status is not JobStatus.COMPLETED
        )
        quotes: list[CapacityActionQuote] = []
        for request in requests:
            preview = company.model_copy(
                update={
                    "capacity_projects": [
                        project.model_copy() for project in company.capacity_projects
                    ]
                }
            )
            try:
                flow = self.apply(preview, request, day)
            except ValueError:
                continue
            receipt = flow.receipts[0]
            assert flow.kind is not None
            quotes.append(
                CapacityActionQuote(
                    action=(
                        "cancel_capacity_project"
                        if request.cancel_capacity_project_id is not None
                        else self._action_name(flow.kind)
                    ),
                    target_tier=receipt.target_tier,
                    project_id=receipt.project_id,
                    charged_cents=flow.charged_cents,
                    refunded_cents=flow.refunded_cents,
                    effective_day=receipt.effective_day,
                )
            )
        return tuple(quotes)

    def apply(self, company: CompanyState, decision: CompanyDecision, day: int) -> CapacityCashFlow:
        """Apply a validated capacity request without changing cash or ledger receipts."""
        if day < 0:
            raise ValueError("capacity action day cannot be negative")
        if decision.company_id != company.company_id:
            raise ValueError("capacity decision belongs to another company")
        actions = (
            decision.capacity_target_tier,
            decision.capacity_downsize_tier,
            decision.cancel_capacity_project_id,
        )
        count = sum(value is not None for value in actions)
        if not count:
            return CapacityCashFlow()
        if count != 1:
            raise ValueError("submit only one capacity action per decision")
        if company.status is not CompanyStatus.ACTIVE:
            raise ValueError("bankrupt companies cannot adjust or cancel capacity")
        if decision.cancel_capacity_project_id is not None:
            return self._cancel(company, decision.cancel_capacity_project_id, day)
        if decision.capacity_target_tier is not None:
            return self._start(
                company, decision.capacity_target_tier, CapacityChangeKind.EXPAND, day
            )
        assert decision.capacity_downsize_tier is not None
        return self._start(
            company, decision.capacity_downsize_tier, CapacityChangeKind.DOWNSIZE, day
        )

    def complete(self, company: CompanyState, day: int) -> tuple[ActionReceipt, ...]:
        """Activate due adjustments once, without touching inventory or outstanding orders."""
        if company.status is not CompanyStatus.ACTIVE:
            return ()
        receipts: list[ActionReceipt] = []
        for project in sorted(company.capacity_projects, key=lambda item: item.project_id):
            if project.status is not JobStatus.PENDING or project.completion_day > day:
                continue
            company.capacity_tier = project.target_tier
            project.status = JobStatus.COMPLETED
            receipts.append(
                ActionReceipt(
                    day=day,
                    action=self._action_name(project.kind),
                    status="completed",
                    message=f"Capacity tier {project.target_tier} is now active.",
                    effective_day=day,
                    project_id=project.project_id,
                    target_tier=project.target_tier,
                )
            )
        return tuple(receipts)

    def _start(
        self, company: CompanyState, target: int, kind: CapacityChangeKind, day: int
    ) -> CapacityCashFlow:
        if target not in self._tiers:
            raise ValueError(f"unknown capacity tier {target}")
        pending = next(
            (
                project
                for project in company.capacity_projects
                if project.status is JobStatus.PENDING
            ),
            None,
        )
        if pending is not None:
            if pending.kind is kind and pending.target_tier == target:
                return self._no_op(
                    day, kind, target, "This capacity adjustment is already pending.", pending
                )
            raise ValueError("another capacity adjustment is pending; finish or cancel it first")
        source = company.capacity_tier
        if target == source:
            return self._no_op(day, kind, target, f"Capacity tier {target} is already active.")
        if kind is CapacityChangeKind.EXPAND and target < source:
            raise ValueError("expansion target must exceed the active tier; use downsize_capacity")
        if kind is CapacityChangeKind.DOWNSIZE and target > source:
            raise ValueError("downsize target must be below the active tier; use expand_capacity")
        tier = self._tiers[target]
        if kind is CapacityChangeKind.EXPAND:
            charge = tier.expansion_cost_cents - self._tiers[source].expansion_cost_cents
            duration = tier.expansion_lead_days
        else:
            charge = (source - target) * self._policy.downsize_fee_per_tier_cents
            duration = self._policy.downsize_lead_days
        if charge < 0:
            raise ValueError("capacity capital prices must increase with tier")
        project = CapacityProject(
            project_id=semantic_digest(
                self._seed,
                "capacity-project",
                company.company_id,
                day,
                kind,
                source,
                target,
                len(company.capacity_projects),
            ).hex()[:24],
            kind=kind,
            source_tier=source,
            target_tier=target,
            started_day=day,
            completion_day=day + duration,
            paid_cents=charge,
        )
        receipt = ActionReceipt(
            day=day,
            action=self._action_name(kind),
            status="applied",
            message=(
                f"Capacity adjustment {source} → {target} starts now; "
                f"the active tier and daily fee change on day {project.completion_day}."
            ),
            charged_cents=charge,
            effective_day=project.completion_day,
            project_id=project.project_id,
            target_tier=target,
        )
        company.capacity_projects.append(project)
        return CapacityCashFlow(charged_cents=charge, receipts=(receipt,), kind=kind)

    def _cancel(self, company: CompanyState, project_id: str, day: int) -> CapacityCashFlow:
        project = next(
            (item for item in company.capacity_projects if item.project_id == project_id), None
        )
        if project is None:
            raise ValueError(f"unknown capacity project {project_id}")
        if project.kind is not CapacityChangeKind.EXPAND:
            raise ValueError("only unfinished expansion projects can be cancelled")
        if project.status is JobStatus.CANCELLED:
            receipt = ActionReceipt(
                day=day,
                action="cancel_capacity_project",
                status="no_op",
                message="This project was already cancelled; no additional refund is due.",
                project_id=project_id,
                target_tier=project.target_tier,
            )
            return CapacityCashFlow(receipts=(receipt,), kind=project.kind)
        if project.status is not JobStatus.PENDING or day >= project.completion_day:
            raise ValueError("completed or due expansion projects cannot be cancelled")
        if day < project.started_day:
            raise ValueError("capacity cancellation cannot precede the project start")
        startup = project.paid_cents * self._policy.startup_cost_bps // 10_000
        duration = project.completion_day - project.started_day
        elapsed = day - project.started_day
        incurred = startup + (project.paid_cents - startup) * elapsed // duration
        fee = min(
            project.paid_cents - incurred,
            project.paid_cents * self._policy.cancellation_fee_bps // 10_000,
        )
        refund = max(0, project.paid_cents - incurred - fee)
        receipt = ActionReceipt(
            day=day,
            action="cancel_capacity_project",
            status="cancelled",
            message=(
                f"Expansion cancelled. Prepaid {project.paid_cents} cents, "
                f"incurred {incurred} cents, cancellation fee {fee} cents; "
                f"refund {refund} cents. No future capacity or fixed fee change."
            ),
            refunded_cents=refund,
            effective_day=day,
            project_id=project_id,
            target_tier=project.target_tier,
        )
        project.status = JobStatus.CANCELLED
        project.cancelled_day = day
        project.refund_cents = refund
        return CapacityCashFlow(refunded_cents=refund, receipts=(receipt,), kind=project.kind)

    @staticmethod
    def _action_name(
        kind: CapacityChangeKind,
    ) -> Literal["expand_capacity", "downsize_capacity"]:
        return "expand_capacity" if kind is CapacityChangeKind.EXPAND else "downsize_capacity"

    def _no_op(
        self,
        day: int,
        kind: CapacityChangeKind,
        target: int,
        message: str,
        project: CapacityProject | None = None,
    ) -> CapacityCashFlow:
        receipt = ActionReceipt(
            day=day,
            action=self._action_name(kind),
            status="no_op",
            message=message,
            project_id=project.project_id if project is not None else None,
            target_tier=target,
            effective_day=project.completion_day if project is not None else day,
        )
        return CapacityCashFlow(receipts=(receipt,), kind=kind)
