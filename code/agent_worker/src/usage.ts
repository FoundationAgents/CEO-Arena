import type { AgentUsage } from "./protocol";

export const EMPTY_USAGE: AgentUsage = Object.freeze({
  provider_turns: 0,
  finalization_turns: 0,
  tool_calls: 0,
  input_tokens: 0,
  output_tokens: 0,
  reasoning_tokens: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  latency_ms: 0,
});

/** Combine monotonic counters without changing the provider's token accounting units. */
export function combineUsage(
  left: AgentUsage,
  right: AgentUsage,
  operation: (a: number, b: number) => number,
): AgentUsage {
  const usage = { ...EMPTY_USAGE };
  for (const key of Object.keys(usage) as (keyof AgentUsage)[])
    usage[key] = operation(left[key], right[key]);
  return usage;
}
