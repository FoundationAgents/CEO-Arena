import { expect, test } from "bun:test";

import { LLMClient } from "@opencode-ai/llm";
import { RequestExecutor } from "@opencode-ai/llm/route";
import { Effect, Layer } from "effect";

import { type CompanyBridge, ToolRejectedError } from "./bridge";
import { TransportRecovery } from "./failure";
import type { CompanyDecision, DecisionRequest, QueryResult } from "./protocol";
import { runDecision } from "./runner";
import { decisionFixture } from "./testing";
import { TraceRecorder } from "./trace";

type Call = { name: string; input: unknown; raw?: string };
type Turn = {
  calls?: Call[];
  text?: string;
  tokens?: number;
  missingUsage?: boolean;
  status?: number;
  dangling?: boolean;
  combinedFinish?: boolean;
};
type Wire = "chat" | "responses" | "messages";

type MessageBody = {
  messages: {
    role: string;
    content: { type: string; id?: string; tool_use_id?: string }[];
  }[];
};

const hasCompleteToolBatches = (body: unknown): boolean => {
  const { messages } = body as MessageBody;
  return messages.every((message, index) => {
    if (message.role !== "assistant") return true;
    const calls = message.content.filter((block) => block.type === "tool_use");
    if (!calls.length) return true;
    const next = messages[index + 1];
    const results =
      next?.content.filter((block) => block.type === "tool_result") ?? [];
    return (
      next?.role === "user" &&
      results.length === calls.length &&
      new Set(results.map((block) => block.tool_use_id)).size ===
        calls.length &&
      calls.every((call) =>
        results.some((result) => result.tool_use_id === call.id),
      )
    );
  });
};

const request = (): DecisionRequest => {
  const fixture = decisionFixture();
  return {
    ...fixture,
    request_id: "r:acme:0",
    request: {
      ...fixture.request,
      run_id: "r",
      company_id: "acme",
      world_version: 0,
      budget: {
        max_provider_turns: 16,
        max_tool_calls: 32,
        max_output_tokens: 1000,
        timeout_seconds: 5,
      },
      observation: {
        ...fixture.request.observation,
        scenario_id: "test",
        world_version: 0,
        day: 0,
        days_remaining: 500,
        company: {
          ...fixture.request.observation.company,
          company_id: "acme",
          cash_cents: 100000,
        },
        public_prices_cents: { acme: { basic: 1000 } },
        public_promotions_cents: { acme: { basic: 0 } },
        public_effective_prices_cents: { acme: { basic: 1000 } },
      },
    },
  };
};

class TestCompany implements CompanyBridge {
  validations: CompanyDecision[] = [];
  async query(): Promise<QueryResult> {
    return { columns: ["value"], rows: [[1]], truncated: false };
  }
  async validate(decision: CompanyDecision) {
    this.validations.push(structuredClone(decision));
  }
}

class Audit extends TraceRecorder {
  readonly events: { kind: string; payload: unknown }[] = [];
  constructor() {
    super("offline-key");
  }
  override record(kind: string, payload: unknown) {
    this.events.push({ kind, payload });
  }
}

const frames = (turn: Turn, wire: Wire): object[] => {
  const calls = turn.calls ?? [];
  const tokens = turn.tokens ?? 5;
  const args = (call: Call) => call.raw ?? JSON.stringify(call.input);
  if (wire === "chat")
    return [
      {
        choices: [
          {
            index: 0,
            delta: {
              content: turn.text,
              tool_calls: calls.map((call, index) => ({
                index,
                id: "call-" + index,
                type: "function",
                function: { name: call.name, arguments: args(call) },
              })),
            },
            finish_reason: turn.combinedFinish ? "tool_calls" : null,
          },
        ],
      },
      ...(turn.dangling || turn.combinedFinish
        ? []
        : [
            {
              choices: [
                {
                  index: 0,
                  delta: {},
                  finish_reason: calls.length ? "tool_calls" : "stop",
                },
              ],
            },
          ]),
      ...(turn.missingUsage
        ? []
        : [
            {
              choices: [],
              usage: {
                prompt_tokens: 10,
                completion_tokens: tokens,
                total_tokens: 10 + tokens,
              },
            },
          ]),
    ];
  if (wire === "responses")
    return [
      {
        type: "response.created",
        response: { id: "response-1", status: "in_progress", model: "gpt-6-astra" },
      },
      ...calls.flatMap((call, index) => {
        const item = {
          type: "function_call",
          id: "item-" + index,
          call_id: "call-" + index,
          name: call.name,
        };
        return [
          {
            type: "response.output_item.added",
            output_index: index,
            item: { ...item, arguments: "" },
          },
          {
            type: "response.function_call_arguments.delta",
            item_id: item.id,
            delta: args(call),
          },
          {
            type: "response.output_item.done",
            output_index: index,
            item: { ...item, arguments: args(call) },
          },
        ];
      }),
      {
        type: "response.completed",
        response: {
          id: "response-1",
          status: "completed",
          ...(turn.missingUsage
            ? {}
            : { usage: { input_tokens: 10, output_tokens: tokens } }),
        },
      },
    ];
  return [
    {
      type: "message_start",
      message: {
        id: "message-1",
        role: "assistant",
        model: "claude-fable-5-1",
        usage: { input_tokens: 10, output_tokens: 0 },
      },
    },
    ...calls.flatMap((call, index) => [
      {
        type: "content_block_start",
        index,
        content_block: {
          type: "tool_use",
          id: "call-" + index,
          name: call.name,
          input: {},
        },
      },
      {
        type: "content_block_delta",
        index,
        delta: { type: "input_json_delta", partial_json: args(call) },
      },
      { type: "content_block_stop", index },
    ]),
    {
      type: "message_delta",
      delta: { stop_reason: calls.length ? "tool_use" : "end_turn" },
      ...(turn.missingUsage ? {} : { usage: { output_tokens: tokens } }),
    },
    { type: "message_stop" },
  ];
};

const seal: Turn = { calls: [{ name: "seal_decision", input: {} }] };
const memo = {
  name: "write_file",
  input: {
    path: "MEMORY.md",
    content:
      "Initial hypothesis: preserve margin; check actual fulfillment next week.",
  },
};
const review: Turn = { calls: [memo] };
const product: Turn = {
  calls: [
    { name: "set_product", input: { product_id: "basic", price_cents: 1500 } },
    memo,
  ],
};

async function execute(
  turns: Turn[],
  wire: Wire = "chat",
  limits: Partial<DecisionRequest["request"]["budget"]> = {},
  company = new TestCompany(),
  input = request(),
) {
  const bodies: unknown[] = [];
  const audit = new Audit();
  const server = Bun.serve({
    hostname: "127.0.0.1",
    port: 0,
    async fetch(incoming) {
      bodies.push(await incoming.json());
      if (wire === "messages" && !hasCompleteToolBatches(bodies.at(-1))) {
        return Response.json(
          { error: "TOOL_USE_RESULT_MISMATCH" },
          { status: 400 },
        );
      }
      const turn = turns[bodies.length - 1];
      if (!turn)
        return new Response("unexpected extra request", { status: 400 });
      if (turn.status)
        return new Response("provider unavailable", { status: turn.status });
      const data = frames(turn, wire)
        .map((frame) => "data: " + JSON.stringify(frame) + "\n\n")
        .join("");
      return new Response(data + (wire === "chat" ? "data: [DONE]\n\n" : ""), {
        headers: { "content-type": "text/event-stream" },
      });
    },
  });
  const message = {
    ...input,
    request: {
      ...input.request,
      budget: { ...input.request.budget, ...limits },
    },
  };
  try {
    const result = await Effect.runPromise(
      runDecision(
        message,
        {
          apiKey: "offline-key",
          baseURL: server.url.origin + "/v1",
          model: {
            chat: "test-model",
            responses: "gpt-6-astra",
            messages: "claude-fable-5-1",
          }[wire],
        },
        company,
        audit,
        new TransportRecovery(0, 0),
      ).pipe(
        Effect.provide(
          LLMClient.layer.pipe(Layer.provide(RequestExecutor.fetchLayer)),
        ),
      ),
    );
    return { result, bodies, audit, company };
  } finally {
    server.stop(true);
  }
}

test("Messages multi-tool results satisfy the strict provider and preserve the budget", async () => {
  const { result, bodies, company } = await execute(
    [
      {
        calls: [
          { name: "query_company", input: { sql: "SELECT 1" } },
          { name: "query_company", input: { sql: "SELECT 2" } },
          memo,
        ],
      },
      seal,
    ],
    "messages",
  );
  expect(result.status).toBe("sealed");
  expect(bodies).toHaveLength(2);
  expect(hasCompleteToolBatches(bodies[1])).toBe(true);
  expect(company.validations).toHaveLength(1);
  expect(result.usage).toMatchObject({
    provider_turns: 2,
    tool_calls: 4,
    output_tokens: 10,
  });
  expect(result.diagnostics).toMatchObject({
    protocol_errors: 0,
    tool_errors: 0,
    recovered: false,
  });
});

for (const wire of ["chat", "responses", "messages"] as const) {
  test(wire + " permits sealing without a weekly memory update", async () => {
    const { result, bodies } = await execute([seal], wire);
    expect(result.status).toBe("sealed");
    expect(bodies).toHaveLength(1);
    expect(result.usage.provider_turns).toBe(1);
  });

  test(wire + " sends object-only schemas for no-argument tools", async () => {
    const { result, bodies } = await execute([review, seal], wire);
    expect(result.status).toBe("sealed");
    type WireTool = {
      name?: string;
      input_schema?: Record<string, unknown>;
      parameters?: Record<string, unknown>;
      function?: { name: string; parameters: Record<string, unknown> };
    };
    for (const name of [
      "preview_decision",
      "reset_decision",
      "seal_decision",
    ]) {
      const tool = (bodies[0] as { tools: WireTool[] }).tools.find(
        (item) => (item.function?.name ?? item.name) === name,
      );
      const schema =
        tool?.function?.parameters ?? tool?.parameters ?? tool?.input_schema;
      expect(schema?.type).toBe("object");
      expect(schema?.additionalProperties).toBe(false);
      for (const keyword of ["anyOf", "oneOf", "allOf"])
        expect(schema).not.toHaveProperty(keyword);
    }
  });

  test(
    wire + " recovers malformed JSON with complete usage and retained draft",
    async () => {
      const { result, bodies } = await execute(
        [
          product,
          {
            calls: [
              { name: "set_product", input: {}, raw: '{"product_id":' },
              {
                name: "set_operations",
                input: {
                  reliability_budget_cents: 999,
                  support_budget_cents: 999,
                },
              },
            ],
          },
          seal,
        ],
        wire,
      );
      expect(result.status).toBe("sealed");
      expect(result.diagnostics).toMatchObject({
        protocol_errors: 1,
        tool_errors: 0,
        recovered: true,
      });
      expect(result.usage).toMatchObject({
        provider_turns: 3,
        tool_calls: 5,
        output_tokens: 15,
      });
      expect(result.decision?.products[0]?.price_cents).toBe(1500);
      expect(result.decision?.reliability_budget_cents).toBeUndefined();
      expect(JSON.stringify(bodies[2])).toContain("Re-emit valid JSON");
    },
  );
  test(
    wire + " rejects mixed seal atomically and repairs within the same window",
    async () => {
      const { result, bodies } = await execute(
        [
          product,
          {
            calls: [
              ...seal.calls!,
              {
                name: "set_product",
                input: { product_id: "basic", price_cents: 1 },
              },
            ],
          },
          seal,
        ],
        wire,
      );
      expect(result.status).toBe("sealed");
      expect(result.decision?.products[0]?.price_cents).toBe(1500);
      expect(result.usage.tool_calls).toBe(5);
      expect(JSON.stringify(bodies[2])).toContain(
        "No call in this response was executed",
      );
    },
  );
}

test("ordinary schema errors and missing tools preserve previous staged actions", async () => {
  const { result } = await execute([
    product,
    { text: "All done." },
    {
      calls: [
        {
          name: "set_product",
          input: { product_id: "basic", price_cents: -1 },
        },
        { name: "not_a_tool", input: {} },
      ],
    },
    seal,
  ]);
  expect(result.status).toBe("sealed");
  expect(result.decision?.products[0]?.price_cents).toBe(1500);
  expect(result.diagnostics.protocol_errors).toBe(3);
});

test("failed seal leaves the draft editable until economic validation succeeds", async () => {
  class RejectsExpansion extends TestCompany {
    override async validate(decision: CompanyDecision) {
      await super.validate(decision);
      if (decision.capacity_target_tier !== undefined)
        throw new ToolRejectedError(
          "authoritative economic validation rejected expansion",
        );
    }
  }
  const { result, company } = await execute(
    [
      product,
      { calls: [{ name: "expand_capacity", input: { target_tier: 2 } }] },
      seal,
      { calls: [{ name: "downsize_capacity", input: { target_tier: 0 } }] },
      seal,
    ],
    "chat",
    {},
    new RejectsExpansion(),
  );
  expect(result.status).toBe("sealed");
  expect(
    company.validations.map((draft) => draft.capacity_target_tier),
  ).toEqual([2, undefined]);
  expect(result.decision?.capacity_downsize_tier).toBe(0);
  expect(result.decision?.products[0]?.price_cents).toBe(1500);
  expect(result.diagnostics).toMatchObject({
    protocol_errors: 0,
    tool_errors: 1,
    recovered: true,
  });
});

test("correction never resets turn, token, or attempted-call budgets", async () => {
  const turns = [{ text: "done" }, { text: "done" }];
  expect(
    (await execute(turns, "chat", { max_provider_turns: 2 })).result.status,
  ).toBe("budget_exhausted");
  expect(
    (await execute(turns, "chat", { max_output_tokens: 10 })).result.usage
      .output_tokens,
  ).toBe(10);
  const mixed = { calls: [...product.calls!, ...seal.calls!] };
  const { result, bodies } = await execute([mixed], "chat", {
    max_tool_calls: 2,
  });
  expect(result.status).toBe("budget_exhausted");
  expect(result.usage.tool_calls).toBe(3);
  expect(bodies).toHaveLength(1);
});

test("missing accounting, incomplete stream, and HTTP failure are not model errors", async () => {
  for (const turn of [
    { ...seal, missingUsage: true },
    { ...seal, dangling: true },
    { status: 401 },
  ]) {
    const { result, bodies } = await execute([turn]);
    expect(result.status).toBe("provider_error");
    expect(result.diagnostics.protocol_errors).toBe(0);
    // The SDK may retry HTTP transport failures internally, but the session
    // must never turn them into a corrective model conversation.
    expect(bodies).toHaveLength("dangling" in turn && turn.dangling ? 2 : 1);
  }
});

test("unexpected validation crashes are infrastructure failures, not model corrections", async () => {
  class BrokenCompany extends TestCompany {
    override async validate() {
      throw new Error("validator implementation crashed");
    }
  }
  const { result } = await execute(
    [review, seal],
    "chat",
    {},
    new BrokenCompany(),
  );
  expect(result.status).toBe("provider_error");
  expect(result.usage.provider_turns).toBe(2);
});

for (const wire of ["chat", "responses", "messages"] as const) {
  test(
    wire + " corrects a missing tool and refuses absent final accounting",
    async () => {
      const fixed = await execute([{ text: "finished" }, review, seal], wire);
      expect(fixed.result.status).toBe("sealed");
      expect(fixed.result.diagnostics.protocol_errors).toBe(1);
      const missing = await execute([{ ...seal, missingUsage: true }], wire);
      expect(missing.result.status).toBe("provider_error");
      expect(missing.company.validations).toHaveLength(0);
    },
  );
}

test("final-frame arguments are accounted without executing malformed sibling calls", async () => {
  const bad: Turn = {
    combinedFinish: true,
    calls: [
      { name: "set_product", input: {}, raw: '{"product_id":' },
      { name: "set_product", input: {}, raw: '{"price_cents":' },
    ],
  };
  const { result } = await execute([
    bad,
    { ...product, combinedFinish: true },
    seal,
  ]);
  expect(result.status).toBe("sealed");
  expect(result.diagnostics.protocol_errors).toBe(2);
  expect(result.usage.tool_calls).toBe(5);
  expect(result.decision?.products[0]?.price_cents).toBe(1500);
});

test("the last allowed turn can seal, but budget overruns never commit drafts", async () => {
  const exact = await execute([product, seal], "chat", {
    max_provider_turns: 2,
    max_tool_calls: 3,
    max_output_tokens: 10,
  });
  expect(exact.result.status).toBe("sealed");
  for (const budget of [{ max_tool_calls: 1 }, { max_output_tokens: 4 }]) {
    const failed = await execute([product], "chat", budget);
    expect(failed.result.status).toBe("budget_exhausted");
    expect(failed.result.decision).toBeUndefined();
    expect(failed.company.validations).toHaveLength(0);
  }
});

test("ordinary rejected actions preserve siblings and empty SQL stays inside the worker", async () => {
  const { result } = await execute([
    {
      calls: [
        {
          name: "set_product",
          input: { product_id: "basic", promotion_cents: 9999 },
        },
        { name: "query_company", input: { sql: "" } },
        ...product.calls!,
      ],
    },
    seal,
  ]);
  expect(result.status).toBe("sealed");
  expect(result.decision?.products[0]?.price_cents).toBe(1500);
  expect(result.decision?.products[0]?.promotion_cents).toBeUndefined();
  expect(result.diagnostics).toMatchObject({
    tool_errors: 1,
    protocol_errors: 1,
    recovered: true,
  });
});

test("unaffordable commitments can be previewed, reset and sealed within one budget", async () => {
  const input = decisionFixture();
  const snapshot = structuredClone(input);
  const { result, bodies, company } = await execute(
    [
      {
        calls: [
          memo,
          { name: "expand_capacity", input: { target_tier: 2 } },
          { name: "start_research_project", input: { tier: 2 } },
          {
            name: "request_market_research",
            input: { segment_id: "value", level: 1 },
          },
        ],
      },
      seal,
      { calls: [{ name: "preview_decision", input: {} }] },
      { calls: [{ name: "reset_decision", input: {} }] },
      {
        calls: [
          ...product.calls!,
          { name: "start_research_project", input: { tier: 1 } },
        ],
      },
      seal,
    ],
    "chat",
    { max_provider_turns: 6, max_tool_calls: 11 },
    new TestCompany(),
    input,
  );

  expect(result.status).toBe("sealed");
  expect(result.usage).toMatchObject({
    provider_turns: 6,
    tool_calls: 11,
    output_tokens: 30,
  });
  expect(result.diagnostics).toMatchObject({
    protocol_errors: 0,
    tool_errors: 1,
    recovered: true,
  });
  expect(JSON.stringify(bodies[3])).toContain("-1500");
  expect(result.decision?.capacity_target_tier).toBeUndefined();
  expect(result.decision?.market_research).toEqual([]);
  expect(result.decision?.research_project_tier).toBe(1);
  expect(result.decision?.products[0]?.price_cents).toBe(1500);
  expect(company.validations).toHaveLength(2);
  expect(input).toEqual(snapshot);
});

for (const wire of ["chat", "responses", "messages"] as const) {
  test(wire + " does not execute reset mixed with seal", async () => {
    const { result } = await execute(
      [
        product,
        { calls: [{ name: "reset_decision", input: {} }, ...seal.calls!] },
        { calls: [{ name: "preview_decision", input: {} }] },
        seal,
      ],
      wire,
    );
    expect(result.status).toBe("sealed");
    expect(result.usage).toMatchObject({
      provider_turns: 4,
      tool_calls: 6,
      output_tokens: 20,
    });
    expect(result.decision?.products[0]?.price_cents).toBe(1500);
    expect(result.diagnostics).toMatchObject({
      protocol_errors: 1,
      tool_errors: 0,
      recovered: true,
    });
  });
}

test("reset cannot restore consumed tool calls or trigger implicit sealing", async () => {
  const { result, company, bodies } = await execute(
    [product, { calls: [{ name: "reset_decision", input: {} }] }, seal],
    "chat",
    { max_tool_calls: 3 },
  );
  expect(result.status).toBe("budget_exhausted");
  expect(result.usage.tool_calls).toBe(3);
  expect(result.decision).toBeUndefined();
  expect(company.validations).toHaveLength(0);
  expect(bodies).toHaveLength(2);
});

test("stage and preview return draft acknowledgements without economic execution", async () => {
  const input = request();
  const snapshot = structuredClone(input);
  const { result, bodies, company } = await execute(
    [
      { calls: [memo, { name: "expand_capacity", input: { target_tier: 2 } }] },
      { calls: [{ name: "preview_decision", input: {} }] },
      seal,
    ],
    "chat",
    {},
    new TestCompany(),
    input,
  );
  expect(result.status).toBe("sealed");
  expect(JSON.stringify(bodies[1])).toContain('\\"executed\\":false');
  expect(JSON.stringify(bodies[2])).toContain("Draft estimate, not execution");
  expect(company.validations).toHaveLength(1);
  expect(input).toEqual(snapshot);
  expect(input.request.observation.company.capacity_projects).toEqual([]);
});

test("transport recovery reuses the same turn and never replays staged or incomplete tools", async () => {
  class CountedCompany extends TestCompany {
    queries = 0;
    override async query() {
      this.queries++;
      return super.query();
    }
  }
  const company = new CountedCompany();
  const query = {
    calls: [{ name: "query_company", input: { sql: "SELECT 1" } }],
  };
  const { result, bodies, audit } = await execute(
    [product, { ...query, dangling: true }, query, seal],
    "chat",
    {},
    company,
  );
  expect(result.status).toBe("sealed");
  expect(bodies[1]).toEqual(bodies[2]);
  expect(company.queries).toBe(1);
  expect(company.validations).toHaveLength(1);
  expect(result.decision?.products[0]?.price_cents).toBe(1500);
  expect(result.usage).toMatchObject({
    provider_turns: 3,
    tool_calls: 4,
    output_tokens: 15,
  });
  expect(result.diagnostics).toMatchObject({
    protocol_errors: 0,
    tool_errors: 0,
    recovered: false,
  });
  expect(audit.events.some((event) => event.kind === "provider-retry")).toBe(
    true,
  );
});

test("permanent HTTP authentication failure is structured and never disguised as a tool correction", async () => {
  const { result, bodies } = await execute([{ status: 401 }]);
  expect(result.status).toBe("provider_error");
  expect(result.failure).toMatchObject({
    category: "auth",
    http_status: 401,
    retryable: false,
  });
  expect(result.failure?.message).toBe("Provider HTTP 401");
  expect(bodies).toHaveLength(1);
});
