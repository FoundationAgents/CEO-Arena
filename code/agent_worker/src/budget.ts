import { Usage } from "@opencode-ai/llm";

import type { AgentUsage, DecisionRequest } from "./protocol";
import { combineUsage, EMPTY_USAGE } from "./usage";

/** Own cumulative allowance and the irreversible transition to four closing replies. */
export class DecisionBudget {
  private readonly started: number;
  private usage?: Usage;
  private providerTurns = 0;
  private finalizationTurns = 0;
  private finalizing = false;
  private toolCalls = 0;

  constructor(
    private readonly limits: DecisionRequest["request"]["budget"],
    private readonly prior?: AgentUsage,
    private readonly now: () => number = () => performance.now(),
  ) {
    this.started = now();
  }

  get closing() {
    this.finalizing ||=
      (this.prior?.finalization_turns ?? 0) > 0 ||
      this.decisionTurns >= Math.max(0, this.limits.max_provider_turns - 4) ||
      (this.limits.max_output_tokens !== null &&
        this.outputTokens >=
          Math.floor(this.limits.max_output_tokens * 0.75)) ||
      (this.limits.timeout_seconds !== null &&
        this.elapsedMs >= this.limits.timeout_seconds * 750);
    return this.finalizing;
  }

  enterClosing() {
    this.finalizing = true;
  }

  private get decisionTurns() {
    return this.providerTurns + (this.prior?.provider_turns ?? 0);
  }

  private get outputTokens() {
    return (this.usage?.outputTokens ?? 0) + (this.prior?.output_tokens ?? 0);
  }

  private get elapsedMs() {
    return this.now() - this.started + (this.prior?.latency_ms ?? 0);
  }

  get turnsExhausted() {
    return this.turnsRemaining === 0;
  }

  get turnsRemaining() {
    return Math.max(
      0,
      Math.min(
        this.limits.max_provider_turns - this.decisionTurns,
        this.closing
          ? 4 - this.finalizationTurns - (this.prior?.finalization_turns ?? 0)
          : Infinity,
      ),
    );
  }

  get remainingTokens() {
    if (this.limits.max_output_tokens === null) return undefined;
    return Math.max(0, this.limits.max_output_tokens - this.outputTokens);
  }

  get remainingMs() {
    return this.limits.timeout_seconds === null
      ? undefined
      : Math.max(0, this.limits.timeout_seconds * 1000 - this.elapsedMs);
  }

  get phaseMs() {
    return this.limits.timeout_seconds === null
      ? undefined
      : Math.max(
          0,
          this.limits.timeout_seconds * (this.closing ? 1000 : 750) -
            this.elapsedMs,
        );
  }

  get phaseTokens() {
    if (this.limits.max_output_tokens === null) return undefined;
    return Math.max(
      0,
      Math.floor(this.limits.max_output_tokens * (this.closing ? 1 : 0.75)) -
        this.outputTokens,
    );
  }

  get exhausted() {
    if (this.remainingMs === 0)
      return "active-time budget exhausted before sealing";
    if (this.turnsExhausted)
      return "provider-turn budget exhausted before sealing";
    if (
      this.limits.max_tool_calls !== null &&
      this.toolCalls + (this.prior?.tool_calls ?? 0) >=
        this.limits.max_tool_calls
    )
      return "tool-call budget exhausted before sealing";
    if (this.remainingTokens === 0)
      return "output-token budget exhausted before sealing";
    return undefined;
  }

  record(usage: Usage | undefined, calls: number, partial = false) {
    this.toolCalls += calls;
    if (!usage) return "provider omitted valid output-token usage";
    const missingOutput = usage.outputTokens === undefined;
    if (missingOutput && !partial)
      return "provider omitted valid output-token usage";
    for (const value of [
      usage.outputTokens,
      usage.inputTokens,
      usage.reasoningTokens,
      usage.cacheReadInputTokens,
      usage.cacheWriteInputTokens,
    ])
      if (value !== undefined && (!Number.isSafeInteger(value) || value < 0))
        return "provider returned invalid token accounting";
    if (!partial) {
      this.providerTurns++;
      if (this.finalizing) this.finalizationTurns++;
    }
    const sum = (key: keyof Usage) => {
      const left = this.usage?.[key];
      const right = usage[key];
      return (
        (typeof left === "number" ? left : 0) +
        (typeof right === "number" ? right : 0)
      );
    };
    this.usage = new Usage({
      inputTokens: sum("inputTokens"),
      outputTokens: sum("outputTokens"),
      reasoningTokens: sum("reasoningTokens"),
      cacheReadInputTokens: sum("cacheReadInputTokens"),
      cacheWriteInputTokens: sum("cacheWriteInputTokens"),
    });
    return missingOutput
      ? "provider omitted valid output-token usage"
      : undefined;
  }

  get exceeded() {
    if (
      this.limits.max_tool_calls !== null &&
      this.toolCalls + (this.prior?.tool_calls ?? 0) >
        this.limits.max_tool_calls
    )
      return "tool-call budget exceeded";
    if (
      this.limits.max_output_tokens !== null &&
      this.outputTokens > this.limits.max_output_tokens
    )
      return "output-token budget exceeded";
    return undefined;
  }

  snapshot(): AgentUsage {
    return {
      provider_turns: this.providerTurns,
      finalization_turns: this.finalizationTurns,
      tool_calls: this.toolCalls,
      input_tokens: this.usage?.inputTokens ?? 0,
      output_tokens: this.usage?.outputTokens ?? 0,
      reasoning_tokens: this.usage?.reasoningTokens ?? 0,
      cache_read_tokens: this.usage?.cacheReadInputTokens ?? 0,
      cache_write_tokens: this.usage?.cacheWriteInputTokens ?? 0,
      latency_ms: Math.max(0, Math.round(this.now() - this.started)),
    };
  }

  get total(): AgentUsage {
    return combineUsage(
      this.prior ?? EMPTY_USAGE,
      this.snapshot(),
      (a, b) => a + b,
    );
  }
}
