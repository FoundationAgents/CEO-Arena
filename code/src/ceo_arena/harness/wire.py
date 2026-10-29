"""Typed NDJSON messages exchanged with isolated agent workers."""

from typing import Literal

from pydantic import Field

from ceo_arena.models import (
    CompanyDecision,
    DecisionWindowRequest,
    DecisionWindowResult,
    StrictModel,
)
from ceo_arena.runtime.context import QueryResult, SqlValue


class WorkerDecisionRequest(StrictModel):
    type: Literal["decide"] = "decide"
    request_id: str = Field(min_length=1)
    request: DecisionWindowRequest


class WorkerShutdownRequest(StrictModel):
    type: Literal["shutdown"] = "shutdown"


class WorkerReady(StrictModel):
    type: Literal["ready"] = "ready"
    company_id: str = Field(min_length=1)
    opencode_commit: str = Field(min_length=40, max_length=40)
    decision_protocol_version: str = Field(min_length=1)


class WorkerDecisionResponse(StrictModel):
    type: Literal["result"] = "result"
    request_id: str = Field(min_length=1)
    result: DecisionWindowResult


class WorkerQueryRequest(StrictModel):
    type: Literal["query"] = "query"
    request_id: str = Field(min_length=1)
    query_id: str = Field(min_length=1)
    sql: str = Field(min_length=1)
    parameters: tuple[SqlValue, ...] = ()
    row_limit: int = Field(default=1_000, gt=0, le=10_000)


class WorkerQueryResponse(StrictModel):
    type: Literal["query_result"] = "query_result"
    request_id: str = Field(min_length=1)
    query_id: str = Field(min_length=1)
    result: QueryResult | None = None
    error: str | None = None


class WorkerValidationRequest(StrictModel):
    type: Literal["validate_decision"] = "validate_decision"
    request_id: str = Field(min_length=1)
    validation_id: str = Field(min_length=1)
    decision: CompanyDecision


class WorkerValidationResponse(StrictModel):
    type: Literal["validation_result"] = "validation_result"
    request_id: str = Field(min_length=1)
    validation_id: str = Field(min_length=1)
    error: str | None = None


def request_id(request: DecisionWindowRequest) -> str:
    """Build a stable idempotency key for a decision window."""

    return f"{request.run_id}:{request.company_id}:{request.world_version}"
