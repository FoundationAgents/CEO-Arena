import { expect, test } from "bun:test";
import { LLMClient, LLMEvent, LLMRequest } from "@opencode-ai/llm";
import { Effect, Layer, Stream } from "effect";

import { ProviderFault } from "./failure";
import { runDecision } from "./runner";
import { decisionFixture } from "./testing";
import { TraceRecorder } from "./trace";

const call = (name: string, input: unknown = {}) =>
  LLMEvent.toolCall({ id: name, name, input });
const finish = (tokens: number) =>
  LLMEvent.finish({ reason: "tool-calls", usage: { outputTokens: tokens } });

async function simulate(
  seal: boolean,
  tokens = 131072,
  timeout: number | null = 0.4,
  model = "offline",
  broken = false,
) {
  const input = decisionFixture();
  const requests: LLMRequest[] = [];
  const client = Layer.succeed(LLMClient.Service, {
    prepare: LLMClient.prepare,
    stream(request: LLMRequest) {
      requests.push(request);
      if (requests.length === 1)
        return Stream.fromIterable([
          call("set_product", { product_id: "basic", price_cents: 1500 }),
          call("write_file", {
            path: "MEMORY.md",
            content: "Hold price 1500; verify next week's fulfillment.",
          }),
          finish(tokens === 131072 ? 10 : Math.floor(tokens * 0.75)),
        ]);
      if (broken)
        return Stream.die(
          new ProviderFault({
            category: "http",
            http_status: 503,
            retryable: true,
            message: "offline provider unavailable",
          }),
        );
      if (seal && (requests.length > 2 || timeout === null))
        return Stream.fromIterable([call("seal_decision"), finish(5)]);
      return Stream.never;
    },
    generate: () => Effect.die("stream only"),
  });
  const started = performance.now();
  const result = await Effect.runPromise(
    runDecision(
      {
        ...input,
        request: {
          ...input.request,
          budget: {
            max_provider_turns: 32,
            max_tool_calls: null,
            max_output_tokens: tokens,
            timeout_seconds: timeout,
          },
        },
      },
      { apiKey: "offline", baseURL: "http://127.0.0.1:1/v1", model },
      {
        query: async () => {
          throw new Error("unexpected query");
        },
        validate: async () => {},
      },
      new TraceRecorder("offline"),
    ).pipe(Effect.provide(client)),
  );
  return { result, requests, elapsed: performance.now() - started };
}

test("soft deadline cancels a stalled stream then seals the original draft in closing", async () => {
  // Leave a full second for closing work on shared CI runners.
  const { result, requests, elapsed } = await simulate(true, 131072, 4);
  expect(result.status).toBe("sealed");
  expect(result.decision?.products[0]?.price_cents).toBe(1500);
  expect(result.usage.finalization_turns).toBe(1);
  expect(result.diagnostics.budget_usage_incomplete).toBe(true);
  expect(requests[2]?.tools.some((tool) => tool.name === "query_company")).toBe(
    true,
  );
  expect(elapsed).toBeLessThan(6000);
}, 10000);

test("hard deadline yields a recorded non-completion, never an implicit draft commit", async () => {
  const { result, elapsed } = await simulate(false);
  expect(result.status).toBe("budget_exhausted");
  expect(result.error).toContain("active-time");
  expect(result.decision).toBeUndefined();
  expect(elapsed).toBeLessThan(1500);
});

test("deadline during repeated provider failures remains an infrastructure error", async () => {
  const { result } = await simulate(false, 131072, 0.4, "offline", true);
  expect(result.status).toBe("provider_error");
  expect(result.failure?.http_status).toBe(503);
  expect(result.decision).toBeUndefined();
});

test("token soft limit reserves finalization without changing the signed tool prefix", async () => {
  const { result, requests } = await simulate(
    true,
    10000,
    null,
    "claude-fable-5-1",
  );
  expect(result.status).toBe("sealed");
  expect(requests.map((request) => request.generation?.maxTokens)).toEqual([
    7500, 2500,
  ]);
  expect(requests[1]?.tools.some((tool) => tool.name === "query_company")).toBe(
    true,
  );
  expect(requests[1]?.tools).toEqual(requests[0]?.tools);
  expect(requests[1]?.system).toEqual(requests[0]?.system);
  expect(result.usage.output_tokens).toBe(7505);
});
