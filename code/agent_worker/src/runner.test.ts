import { expect, test } from "bun:test";

import { LLM, LLMClient, LLMEvent, LLMRequest } from "@opencode-ai/llm";
import * as OpenAICompatible from "@opencode-ai/llm/providers/openai-compatible";
import { Effect, Layer, Stream } from "effect";

import { remainingGeneration, runDecision, SYSTEM_PROMPT } from "./runner";
import { decisionFixture } from "./testing";
import { TraceRecorder } from "./trace";

test("preserves typed generation options while reducing the output budget", () => {
  const model = OpenAICompatible.configure({
    apiKey: "test-key",
    baseURL: "http://127.0.0.1:1/v1",
    provider: "test",
  }).model("test-model");
  const request = LLM.request({
    model,
    prompt: "test",
    generation: { maxTokens: 8192, seed: 0, temperature: 0 },
  });

  const generation = remainingGeneration(request, 7582);

  expect(generation?.maxTokens).toBe(7582);
  expect(generation?.temperature).toBe(0);
  expect(generation?.seed).toBe(0);
});

const call = (name: string, input: unknown = {}, id = name) =>
  LLMEvent.toolCall({ id, name, input });
const finish = (outputTokens = 10) =>
  LLMEvent.finish({ reason: "tool-calls", usage: { outputTokens } });
const note = () =>
  call("write_file", {
    path: "MEMORY.md",
    content: "Observed cash; hold policy; check fulfillment next week.",
  });

const simulate = async (
  turns: readonly (readonly LLMEvent[])[],
  budget: Partial<ReturnType<typeof decisionFixture>["request"]["budget"]> = {},
  model = "offline",
) => {
  const message = decisionFixture();
  const requests: LLMRequest[] = [];
  const client = Layer.succeed(LLMClient.Service, {
    prepare: LLMClient.prepare,
    stream: (request: LLMRequest) => {
      requests.push(request);
      return Stream.fromIterable(turns[requests.length - 1] ?? []);
    },
    generate: () => Effect.die("offline runner should only stream"),
  });
  const result = await Effect.runPromise(
    runDecision(
      {
        ...message,
        request: {
          ...message.request,
          budget: { ...message.request.budget, ...budget },
        },
      },
      { apiKey: "offline", baseURL: "http://127.0.0.1:1/v1", model },
      {
        query: async () => {
          throw new Error("unexpected database call");
        },
        validate: async () => {},
      },
      new TraceRecorder("offline"),
    ).pipe(Effect.provide(client)),
  );
  return { result, requests };
};

test("Doubao caps total response output at 64K without renewing the window allowance", async () => {
  const { result, requests } = await simulate([
    [call("set_product", { product_id: "basic", price_cents: 1500 }), finish(32_768)],
    [call("preview_decision"), finish(32_768)],
    [note(), finish(32_768)],
    [call("seal_decision"), finish(100)],
  ], {
    max_provider_turns: 32, max_tool_calls: null, max_output_tokens: 131_072,
  }, "doubao-seed-1.8");
  expect(requests.map((request) => request.generation?.maxTokens)).toEqual([
    65_536, 65_536, 32_768, 32_768,
  ]);
  expect(requests[3]?.tools).toEqual(requests[0]?.tools);
  expect(result.status).toBe("sealed");
  expect(result.decision?.products[0]?.price_cents).toBe(1500);
  expect(result.usage).toMatchObject({
    output_tokens: 98_404, provider_turns: 4, finalization_turns: 1,
  });
});

test("Doubao uses a smaller remaining budget instead of resetting its response allowance", async () => {
  const { result, requests } = await simulate([
    [note(), finish(29_999)],
    [call("seal_decision"), finish(1)],
  ], { max_provider_turns: 32, max_output_tokens: 40_000 }, "doubao-seed-1.8");
  expect(requests.map((request) => request.generation?.maxTokens)).toEqual([30_000, 1]);
  expect(result.status).toBe("sealed");
  expect(result.usage.output_tokens).toBe(30_000);
});

test("finalization retains tool definitions but never executes a further query", async () => {
  const { result, requests } = await simulate([
    [note(), finish(75)],
    [call("query_company", { sql: "SELECT 1" }), finish(1)],
    [call("seal_decision"), finish(1)],
  ], { max_provider_turns: 32, max_output_tokens: 100 });
  expect(result.status).toBe("sealed");
  expect(result.diagnostics.protocol_errors).toBe(1);
  expect(requests[1]?.tools).toEqual(requests[0]?.tools);
  expect(requests[2]?.tools).toEqual(requests[0]?.tools);
  expect(JSON.stringify(requests[2]?.messages)).toContain("disabled during finalization");
});

test("Doubao keeps the response cap when reply count triggers early finalization", async () => {
  const { result, requests } = await simulate([
    ...Array.from({ length: 28 }, (_, index) => [
      call("set_product", { product_id: "basic", price_cents: 1000 + index }, `price-${index}`),
      finish(1),
    ]),
    [call("seal_decision"), finish(1)],
  ], {
    max_provider_turns: 32, max_tool_calls: null, max_output_tokens: 131_072,
  }, "doubao-seed-1.8");
  expect(requests).toHaveLength(29);
  expect(requests.every((request) => request.generation?.maxTokens === 65_536)).toBe(true);
  expect(requests[28]?.tools).toEqual(requests[0]?.tools);
  expect(result.status).toBe("sealed");
  expect(result.usage).toMatchObject({ provider_turns: 29, finalization_turns: 1 });
});

test("mixed sealing is atomically rejected and repaired within the original budget", async () => {
  const { result, requests } = await simulate([
    [
      call("set_product", { product_id: "basic", price_cents: 250 }),
      note(),
      finish(),
    ],
    [
      call(
        "set_product",
        { product_id: "basic", price_cents: 350 },
        "bad-change",
      ),
      call("seal_decision"),
      finish(),
    ],
    [call("seal_decision"), finish()],
  ]);
  expect(result.status).toBe("sealed");
  expect(result.decision?.products[0]?.price_cents).toBe(250);
  expect(result.usage).toMatchObject({
    provider_turns: 3,
    tool_calls: 5,
    output_tokens: 30,
  });
  expect(requests.map((request) => request.generation?.maxTokens)).toEqual([
    75, 90, 80,
  ]);
  expect(JSON.stringify(requests[2]?.messages)).toContain(
    "No call in this response was executed",
  );
});

test("text-only turns get a correction, not an automatic empty economic decision", async () => {
  const { result, requests } = await simulate([
    [LLMEvent.finish({ reason: "stop", usage: { outputTokens: 10 } })],
    [note(), finish()],
    [call("seal_decision"), finish()],
  ]);
  expect(result.status).toBe("sealed");
  expect(result.usage.provider_turns).toBe(3);
  expect(JSON.stringify(requests[1]?.messages)).toContain("No tool was called");
});

test("missing output usage is a provider error, not a model economic no-op", async () => {
  const { result } = await simulate([
    [call("seal_decision"), LLMEvent.finish({ reason: "tool-calls" })],
  ]);
  expect(result.status).toBe("provider_error");
  expect(result.decision).toBeUndefined();
  expect(result.usage.output_tokens).toBe(0);
  expect(result.error).toContain("omitted valid output-token usage");
});

test("repairs cannot renew token, turn or tool budgets", async () => {
  const empty = [
    LLMEvent.finish({ reason: "stop", usage: { outputTokens: 10 } }),
  ];
  const tokens = await simulate([empty], { max_output_tokens: 10 });
  expect(tokens.result.error).toContain("output-token budget exhausted");
  expect(tokens.requests).toHaveLength(1);
  const turns = await simulate([empty], { max_provider_turns: 1 });
  expect(turns.result.error).toContain("provider-turn budget exhausted");
  const tools = await simulate(
    [
      [
        call("write_file", { path: "MEMORY.md", content: "rejected" }),
        call("seal_decision"),
        finish(),
      ],
      [call("seal_decision"), finish()],
    ],
    { max_tool_calls: 2 },
  );
  expect(tools.result.error).toContain("tool-call budget exhausted");
  expect(tools.result.usage.tool_calls).toBe(2);
  expect(tools.requests).toHaveLength(1);
  expect(tools.result.decision).toBeUndefined();
});

test("an unaffordable seal stays editable and can be corrected in-budget", async () => {
  const { result } = await simulate([
    [
      call("expand_capacity", { target_tier: 2 }),
      call("start_research_project", { tier: 2 }),
      note(),
      finish(),
    ],
    [call("seal_decision"), finish()],
    [call("start_research_project", { tier: 1 }), finish()],
    [call("seal_decision"), finish()],
  ]);
  expect(result.status).toBe("sealed");
  expect(result.usage.provider_turns).toBe(4);
  expect(result.decision?.research_project_tier).toBe(1);
});

test("freezes recurring and one-time action semantics in the system prompt", () => {
  expect(SYSTEM_PROMPT).toContain("charged every simulated day");
  expect(SYSTEM_PROMPT).toContain("Set a recurring budget to 0 to stop it");
  expect(SYSTEM_PROMPT).toContain("preserves all current recurring policies");
  expect(SYSTEM_PROMPT).toContain(
    "one-time projects, bids, and market-research requests",
  );
  expect(SYSTEM_PROMPT).toContain("product-quality innovation investment");
  expect(SYSTEM_PROMPT).toContain("does not improve product quality");
});

const unlimited = {
  max_provider_turns: 32,
  max_tool_calls: null,
  max_output_tokens: null,
  timeout_seconds: null,
};

test("decision replies end at 32 without a cumulative output cap or an implicit seal", async () => {
  const turns = Array.from({ length: 40 }, (_, index) => [
    ...(index === 0 ? [note()] : []),
    ...Array.from({ length: 4 }, (_, tool) =>
      call(
        "set_product",
        {
          product_id: "basic",
          price_cents: 1000 + index,
        },
        `call-${index}-${tool}`,
      ),
    ),
    finish(1000),
  ]);
  const { result, requests } = await simulate(
    [...turns, [call("seal_decision"), finish(1000)]],
    unlimited,
  );
  expect(result.status).toBe("budget_exhausted");
  expect(result.decision).toBeUndefined();
  expect(result.usage).toMatchObject({
    provider_turns: 32,
    tool_calls: 129,
    output_tokens: 32000,
  });
  expect(
    requests.every((request) => request.generation?.maxTokens === undefined),
  ).toBe(true);
});

test("a provider response limit continues the same draft but cannot implicitly seal", async () => {
  const { result, requests } = await simulate(
    [
      [
        call("set_product", { product_id: "basic", price_cents: 1500 }),
        note(),
        finish(1000),
      ],
      [
        call("seal_decision"),
        LLMEvent.finish({ reason: "length", usage: { outputTokens: 9000 } }),
      ],
      [call("seal_decision"), finish(1000)],
    ],
    unlimited,
  );
  expect(result.status).toBe("sealed");
  expect(result.decision?.products[0]?.price_cents).toBe(1500);
  expect(result.usage).toMatchObject({
    provider_turns: 3,
    output_tokens: 11000,
  });
  expect(JSON.stringify(requests[2]?.messages)).toContain(
    "single-response output limit",
  );
});

test("reasoning-only length replies continue without an invalid assistant message or renewed allowance", async () => {
  const { result, requests } = await simulate([
    [call("set_product", { product_id: "basic", price_cents: 1500 }), note(), finish(10)],
    [
      LLMEvent.reasoningStart({ id: "thought" }),
      LLMEvent.reasoningDelta({ id: "thought", text: "UNFINISHED_REASONING" }),
      LLMEvent.reasoningEnd({ id: "thought" }),
      LLMEvent.finish({ reason: "length", usage: { outputTokens: 20 } }),
    ],
    [call("seal_decision"), finish(10)],
  ]);
  expect(result.status).toBe("sealed");
  expect(result.decision?.products[0]?.price_cents).toBe(1500);
  expect(result.usage).toMatchObject({ provider_turns: 3, output_tokens: 40, tool_calls: 3 });
  expect(JSON.stringify(requests[2]?.messages)).not.toContain("UNFINISHED_REASONING");
  expect(JSON.stringify(requests[2]?.messages)).toContain("single-response output limit");
  expect(requests[2]?.generation?.maxTokens).toBe(70);
});
