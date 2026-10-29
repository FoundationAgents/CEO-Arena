"""Provider retries bounded by the shared decision allowance."""

from typing import ClassVar

from pydantic import Field

from ceo_arena.models import ProviderFailure, StrictModel


class ProviderRetryPolicy(StrictModel):
    """Classify recoverable failures and choose their cancellable backoff."""

    initial_delay_seconds: float = Field(default=1, ge=0)
    maximum_delay_seconds: float = Field(default=60, ge=0)
    HTTP_400_ATTEMPTS: ClassVar[int] = 3

    def record_failure(
        self, failure: ProviderFailure, previous: ProviderFailure | None, *, made_progress: bool
    ) -> ProviderFailure:
        """Count consecutive 400s for the same logical request, including the first."""
        if failure.http_status != 400 or failure.category in {"auth", "identity"}:
            return failure
        repeated = previous is not None and previous.http_status == 400 and not made_progress
        attempt = (previous.attempt or 1) + 1 if repeated else 1
        return failure.model_copy(
            update={"attempt": attempt, "retryable": attempt < self.HTTP_400_ATTEMPTS}
        )

    def can_retry(self, failure: ProviderFailure) -> bool:
        if failure.category in {"auth", "identity"}:
            return False
        status = failure.http_status
        if status == 400:
            return (failure.attempt or 1) < self.HTTP_400_ATTEMPTS
        if not failure.retryable:
            return False
        return (
            status is None
            or status in {408, 409, 425, 429}
            or (status >= 500 and status not in {501, 505})
        )

    def delay(self, attempt: int, failure: ProviderFailure) -> float:
        base = min(
            self.maximum_delay_seconds,
            self.initial_delay_seconds * 2 ** min(max(0, attempt - 1), 16),
        )
        if not self.can_retry(failure):
            raise ValueError("permanent provider failures cannot be retried")
        return max(base, failure.retry_after_seconds or 0)
