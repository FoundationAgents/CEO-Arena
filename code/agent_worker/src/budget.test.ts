import { expect, test } from "bun:test";
import { Usage } from "@opencode-ai/llm";

import { DecisionBudget } from "./budget";

const limits = {
  max_provider_turns: 32,
  max_tool_calls: null,
  max_output_tokens: 131072,
  timeout_seconds: 3600,
};

test("96K or 45 minutes closes irreversibly; four replies share the remaining hard allowance", () => {
  let now = 0;
  const budget = new DecisionBudget(limits, undefined, () => now);
  expect(budget.phaseTokens).toBe(98304);
  budget.record(new Usage({ outputTokens: 98304, reasoningTokens: 98000 }), 0);
  expect(budget.closing).toBe(true);
  expect(budget.phaseTokens).toBe(32768);
  for (let turn = 0; turn < 4; turn++)
    budget.record(new Usage({ outputTokens: 1 }), 0);
  expect(budget.exhausted).toContain("provider-turn");
  expect(budget.snapshot().finalization_turns).toBe(4);
  // Reasoning is a subset of output, not a second charge.
  expect(budget.snapshot().output_tokens).toBe(98308);
  const clock = new DecisionBudget(limits, undefined, () => now);
  now = 2700000;
  expect(clock.closing).toBe(true);
  expect(clock.phaseMs).toBe(900000);
  now = 3600000;
  expect(clock.exhausted).toContain("active-time");
});

test("Partial output and reported retry usage cannot renew tokens, time or closing replies", () => {
  const original = new DecisionBudget(limits, undefined, () => 0);
  original.record(new Usage({ outputTokens: 98304 }), 0, true);
  expect(original.closing).toBe(true);
  original.record(new Usage({ outputTokens: 1000 }), 0);
  const prior = { ...original.snapshot(), latency_ms: 2800000 };
  const resumed = new DecisionBudget(limits, prior, () => 0);
  expect(resumed.closing).toBe(true);
  expect(resumed.turnsRemaining).toBe(3);
  expect(resumed.phaseTokens).toBe(31768);
  expect(resumed.phaseMs).toBe(800000);
  expect(resumed.snapshot().output_tokens).toBe(0); // Attempts report deltas only.
});
