"""One persistent, identity-independent market phase and its delayed public signals."""

from __future__ import annotations

from ceo_arena.models import (
    MarketPhaseConfig,
    MarketState,
    PublicMarketSignal,
    ScenarioConfig,
    SegmentConfig,
    WorldState,
)

from .randomness import NamedRandom


class MarketDynamics:
    """Derive customer conditions from frozen parameters, never company actions or scores."""

    def __init__(self, scenario: ScenarioConfig) -> None:
        self._config = scenario.market_dynamics
        self._segments = scenario.segments
        self._random = NamedRandom(scenario.seed)
        self._neutral = MarketPhaseConfig(phase_id="stationary")
        self._cached_key: tuple[int, int] | None = None
        self._cached_segments: tuple[SegmentConfig, ...] = ()

    def initialize(self, state: WorldState) -> None:
        state.market = MarketState(phase_index=self._config.initial_phase)
        self._publish(state, 0)

    def advance(self, state: WorldState, day: int) -> None:
        """Advance once per day, with no mutable RNG state to lose during recovery."""
        if day != state.market.day + 1:
            raise ValueError("market dynamics must advance exactly one day")
        if self._config.phases:
            row = self._config.transitions[state.market.phase_index]
            chosen = int(self._random.generator("market-phase", day).choice(len(row), p=row))
            if chosen != state.market.phase_index:
                state.market.phase_index = chosen
                state.market.phase_started_day = day
        state.market.day = day
        self._publish(state, day)

    def segment(self, state: WorldState, segment_id: str) -> SegmentConfig:
        segments = self._effective_segments(state.market.phase_index, state.market.day)
        return next(segment for segment in segments if segment.segment_id == segment_id)

    def public_signal(self, state: WorldState) -> PublicMarketSignal:
        day = max(0, state.day - self._config.publication_delay_days)
        return state.market_signals.get(day, PublicMarketSignal(day=day)).model_copy()

    def _phase(self, index: int) -> MarketPhaseConfig:
        if not self._config.phases:
            return self._neutral
        return self._config.phases[index]

    def _effective_segments(self, index: int, day: int) -> tuple[SegmentConfig, ...]:
        if self._cached_key == (index, day):
            return self._cached_segments
        phase = self._phase(index)
        total = sum(segment.base_daily_demand for segment in self._segments)
        weighted = sum(
            segment.base_daily_demand * phase.segment_weights.get(segment.segment_id, 1.0)
            for segment in self._segments
        )
        drift = min(self._config.maximum_quality_drift, day * self._config.daily_quality_drift)
        segments = tuple(
            segment.model_copy(
                update={
                    "base_daily_demand": total
                    * phase.demand_multiplier
                    * segment.base_daily_demand
                    * phase.segment_weights.get(segment.segment_id, 1.0)
                    / weighted,
                    "price_weight": segment.price_weight * phase.price_weight_multiplier,
                    "quality_weight": segment.quality_weight * phase.quality_weight_multiplier,
                    "service_weight": segment.service_weight * phase.service_weight_multiplier,
                    "expected_quality": segment.expected_quality
                    + drift
                    + phase.quality_expectation_shift,
                }
            )
            for segment in self._segments
        )
        self._cached_key = (index, day)
        self._cached_segments = segments
        return segments

    def _publish(self, state: WorldState, day: int) -> None:
        phase = self._phase(state.market.phase_index)
        shift = min(self._config.maximum_quality_drift, day * self._config.daily_quality_drift)
        state.market_signals[day] = PublicMarketSignal(
            day=day,
            demand_index=phase.demand_multiplier * state.macro_index,
            quality_expectation_index=1.0 + shift + phase.quality_expectation_shift,
        )
