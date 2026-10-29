import { expect, test } from "bun:test";

import {
  LLMClient,
  LLMEvent,
  LLMRequest,
  LLMResponse,
  Message,
  ToolDefinition,
} from "@opencode-ai/llm";
import { RequestExecutor } from "@opencode-ai/llm/route";
import { Effect, Layer, Schema } from "effect";

import { fableProtocol } from "./fable";
import {
  type InferenceSettings,
  resolveInference,
  responseTokenLimit,
} from "./inference";
import { chatProtocol, responsesProtocol } from "./native-protocols";
import { NewApiModel } from "./provider";
import { remainingGeneration } from "./runner";

const tool = ToolDefinition.make({
  name: "query_company",
  description: "Query the private company database.",
  inputSchema: {
    type: "object",
    properties: { sql: { type: "string" } },
    required: ["sql"],
    additionalProperties: false,
  },
});

const request = (id: string) =>
  new NewApiModel({
    apiKey: "offline-test-key",
    baseURL: "http://127.0.0.1:1/v1",
    model: id,
  }).request({
    system: "Use query_company, then seal the decision in a separate turn.",
    prompt: "Inspect the company.",
    tools: [tool],
    maxOutputTokens: 8192,
  });

const formalProfiles = [
  [
    "gpt-6-astra",
    "max_output_tokens",
    128_000,
    { reasoning: { effort: "max" } },
  ],
  [
    "gpt-5.6-sol",
    "max_output_tokens",
    128_000,
    { reasoning: { effort: "max" } },
  ],
  [
    "claude-fable-5-1",
    "max_tokens",
    128_000,
    { thinking: { type: "adaptive" }, output_config: { effort: "max" } },
  ],
  [
    "claude-sonnet-5",
    "max_tokens",
    128_000,
    { thinking: { type: "adaptive" }, output_config: { effort: "max" } },
  ],
  [
    "deepseek-v4-pro",
    "max_tokens",
    393_216,
    { thinking: { type: "enabled" }, reasoning_effort: "max" },
  ],
  [
    "deepseek-v4-flash",
    "max_tokens",
    393_216,
    { thinking: { type: "enabled" }, reasoning_effort: "max" },
  ],
  [
    "qwen3.8-max",
    "max_completion_tokens",
    65_536,
    {
      enable_thinking: true,
      preserve_thinking: true,
      reasoning_effort: "xhigh",
    },
  ],
  ["gemini-3.8-flash", "max_tokens", 65_536, { reasoning_effort: "high" }],
  [
    "doubao-seed-1.8",
    "max_completion_tokens",
    65_536,
    { thinking: { type: "enabled" }, reasoning_effort: "high" },
  ],
] as const;

test.each(formalProfiles)(
  "%s sends its highest native inference settings and total output limit",
  async (id, field, limit, inference) => {
    const model = new NewApiModel({
      apiKey: "offline",
      baseURL: "http://127.0.0.1:1/v1",
      model: id,
    });
    expect(model.singleResponseLimit).toBe(limit);
    expect(model.inference).toEqual(resolveInference(id));
    const native = model.request({
      system: "test",
      prompt: "test",
      tools: [tool],
    });
    for (const remaining of [524_288, 98_304, 32_768, 1024, 1]) {
      const bounded = LLMRequest.update(native, {
        generation: remainingGeneration(native, remaining),
      });
      const { body } = await prepare(bounded);
      expect(body).toMatchObject({
        model: id,
        [field]: Math.min(remaining, limit),
        ...inference,
      });
      for (const key of [
        "temperature",
        "top_p",
        "top_k",
        "seed",
        "thinking_budget",
      ])
        expect(body).not.toHaveProperty(key);
      for (const key of [
        "max_tokens",
        "max_output_tokens",
        "max_completion_tokens",
      ])
        if (key !== field) expect(body).not.toHaveProperty(key);
    }
  },
);

test("unknown models do not inherit a guessed inference setting", async () => {
  const model = new NewApiModel({
    apiKey: "offline",
    baseURL: "http://127.0.0.1:1/v1",
    model: "offline-custom",
  });
  const { body } = await prepare(
    model.request({ system: "test", prompt: "test", tools: [tool] }),
  );
  expect(model.singleResponseLimit).toBeUndefined();
  expect(model.inference.expected_model_ids).toEqual([]);
  for (const key of [
    "reasoning_effort",
    "thinking",
    "enable_thinking",
    "max_tokens",
  ])
    expect(body).not.toHaveProperty(key);
});

test("an unbounded window still observes each model's single response capacity", async () => {
  for (const [model, field, limit] of formalProfiles) {
    const native = new NewApiModel({
      apiKey: "offline",
      baseURL: "http://127.0.0.1:1/v1",
      model,
    }).request({ system: "test", prompt: "test", tools: [tool] });
    const prepared = await prepare(native);
    expect(native.generation?.maxTokens).toBe(limit);
    expect(prepared.body).toMatchObject({ [field]: limit });
  }
});

test("explicit settings are frozen, and empty overrides cannot disable known identity checks", async () => {
  const original = resolveInference("gpt-5.6-sol");
  const supplied = {
    ...original,
    effort: "high" as const,
    expected_model_ids: [],
  };
  const model = new NewApiModel({
    apiKey: "offline",
    baseURL: "http://127.0.0.1:1/v1",
    model: "gpt-5.6-sol",
    inference: supplied,
  });
  expect(model.inference.expected_model_ids).toEqual(["gpt-5.6-sol"]);
  expect(Object.isFrozen(model.inference)).toBe(true);
  expect(Object.isFrozen(model.inference.expected_model_ids)).toBe(true);
  const { body } = await prepare(
    model.request({ system: "test", prompt: "test", tools: [tool] }),
  );
  expect(body).toMatchObject({ reasoning: { effort: "high" } });
  expect(resolveInference("gpt-5.6-sol").effort).toBe("max");
});

test("invalid inference combinations are rejected before constructing a provider request", () => {
  const original = resolveInference("gpt-5.6-sol");
  const invalid: ReadonlyArray<Partial<InferenceSettings>> = [
    { max_output_tokens: 0 },
    { max_output_tokens: 1.5 },
    { max_output_tokens: Infinity },
    { expected_model_ids: ["valid", "valid"] },
    { expected_model_ids: ["model with spaces"] },
    { expected_model_ids: ["model\u0000"] },
    { thinking: "enabled" },
    { enable_thinking: true },
    { output_token_field: "max_completion_tokens" },
  ];
  for (const patch of invalid)
    expect(() =>
      resolveInference("gpt-5.6-sol", { ...original, ...patch }),
    ).toThrow();
  expect(() =>
    resolveInference("claude-sonnet-5", {
      ...resolveInference("claude-sonnet-5"),
      effort: "none",
    }),
  ).toThrow();
  expect(() =>
    resolveInference("deepseek-v4-pro", {
      ...resolveInference("deepseek-v4-pro"),
      enable_thinking: true,
    }),
  ).toThrow();
  const extra = { ...original, unsupported: true };
  expect(() => resolveInference("gpt-5.6-sol", extra)).toThrow();
  for (const remaining of [0, -1, 0.5, Infinity, NaN])
    expect(() => responseTokenLimit(original, remaining)).toThrow();
});

test("wire bodies reject undeclared extension fields and incompatible output fields", async () => {
  const inputs = [
    [
      "deepseek-v4-pro",
      Schema.is(chatProtocol(resolveInference("deepseek-v4-pro")).body.schema),
    ],
    [
      "gpt-6-astra",
      Schema.is(responsesProtocol(resolveInference("gpt-6-astra")).body.schema),
    ],
    ["claude-fable-5-1", Schema.is(fableProtocol.body.schema)],
  ] as const;
  for (const [id, valid] of inputs) {
    const body = (await prepare(request(id))).body;
    expect(valid(body)).toBe(true);
    expect(valid({ ...Object(body), arbitrary_option: true })).toBe(false);
  }
  const chat = chatProtocol(resolveInference("qwen3.8-max"));
  const body = (await prepare(request("qwen3.8-max"))).body;
  expect(Schema.is(chat.body.schema)({ ...Object(body), max_tokens: 10 })).toBe(
    false,
  );
});

test.each([
  "deepseek-v4-pro",
  "deepseek-v4-flash",
  "qwen3.8-max",
  "doubao-seed-1.8",
])(
  "%s preserves reasoning content and tool results on the next request",
  async (id) => {
    const input = request(id);
    const response = LLMResponse.fromEvents([
      LLMEvent.reasoningStart({ id: "r0" }),
      LLMEvent.reasoningDelta({
        id: "r0",
        text: "private reasoning, unchanged",
      }),
      LLMEvent.reasoningEnd({ id: "r0" }),
      LLMEvent.toolCall({
        id: "A",
        name: "query_company",
        input: { sql: "SELECT 1" },
      }),
      LLMEvent.finish({ reason: "tool-calls", usage: { outputTokens: 25 } }),
    ])!;
    const { body } = await prepare(
      LLMRequest.update(input, {
        messages: [...input.messages, response.message, reply("A")],
      }),
    );
    expect(body).toMatchObject({
      messages: expect.arrayContaining([
        expect.objectContaining({
          role: "assistant",
          reasoning_content: "private reasoning, unchanged",
          tool_calls: [expect.objectContaining({ id: "A" })],
        }),
        expect.objectContaining({ role: "tool", tool_call_id: "A" }),
      ]),
    });
  },
);

const prepare = async (input: LLMRequest) => {
  const prepared = await Effect.runPromise(
    LLMClient.prepare(input).pipe(
      Effect.provide(
        LLMClient.layer.pipe(Layer.provide(RequestExecutor.fetchLayer)),
      ),
    ),
  );
  // Inspect the actual JSON wire body; undefined SDK fields are not transmitted.
  return {
    ...prepared,
    body: JSON.parse(JSON.stringify(prepared.body)) as unknown,
  };
};

const batch = (...ids: string[]) =>
  Message.assistant(
    ids.map((id) => ({
      type: "tool-call" as const,
      id,
      name: "query_company",
      input: { sql: "SELECT 1" },
    })),
  );

const reply = (id: string, failed = false) =>
  Message.tool({
    id,
    name: "query_company",
    result: failed ? "SQL rejected" : { value: id },
    ...(failed ? { resultType: "error" as const } : {}),
  });

const conversation = (...messages: Message[]) => {
  const input = request("claude-fable-5-1");
  return LLMRequest.update(input, {
    messages: [...input.messages, ...messages],
  });
};

test("Fable groups all results directly after their tool batch without changing input", async () => {
  const input = conversation(batch("A", "B"), reply("A"), reply("B", true));
  const before = JSON.stringify(input);
  const { body } = await prepare(input);
  expect(body).toMatchObject({
    messages: [
      { role: "user" },
      {
        role: "assistant",
        content: [
          { type: "tool_use", id: "A" },
          { type: "tool_use", id: "B" },
        ],
      },
      {
        role: "user",
        content: [
          { type: "tool_result", tool_use_id: "A", content: '{"value":"A"}' },
          {
            type: "tool_result",
            tool_use_id: "B",
            content: "SQL rejected",
            is_error: true,
          },
        ],
      },
    ],
  });
  expect(JSON.stringify(input)).toBe(before);
  expect((await prepare(input)).body).toEqual(body);

  const alreadyGrouped = conversation(
    batch("A", "B"),
    Message.make({
      role: "tool",
      content: [...reply("A").content, ...reply("B", true).content],
    }),
  );
  expect((await prepare(alreadyGrouped)).body).toEqual(body);
});

test("Fable preserves ordinary user messages and separates successive tool batches", async () => {
  const { body } = await prepare(
    conversation(
      batch("A"),
      reply("A"),
      Message.user("Keep the policy"),
      Message.user("Do not invest"),
      batch("A", "B"),
      reply("A"),
      reply("B"),
    ),
  );
  expect(body).toMatchObject({
    messages: [
      { role: "user" },
      { role: "assistant", content: [{ id: "A" }] },
      { role: "user", content: [{ tool_use_id: "A" }] },
      { role: "user", content: [{ type: "text", text: "Keep the policy" }] },
      { role: "user", content: [{ type: "text", text: "Do not invest" }] },
      { role: "assistant", content: [{ id: "A" }, { id: "B" }] },
      { role: "user", content: [{ tool_use_id: "A" }, { tool_use_id: "B" }] },
    ],
  });
});

for (const [label, messages] of [
  ["missing result at end", [batch("A", "B"), reply("A")]],
  ["missing before next assistant", [batch("A"), Message.assistant("Done")]],
  [
    "ordinary text interrupting results",
    [batch("A", "B"), reply("A"), Message.user("Note"), reply("B")],
  ],
  ["duplicate result", [batch("A", "B"), reply("A"), reply("A"), reply("B")]],
  ["unknown result", [batch("A"), reply("other")]],
  ["orphan result", [reply("A")]],
  ["duplicate call", [batch("A", "A"), reply("A")]],
] as const) {
  test("Fable rejects " + label + " before sending a request", async () => {
    const input = conversation(...messages);
    const before = JSON.stringify(input);
    await expect(prepare(input)).rejects.toThrow("Fable tool history:");
    expect(JSON.stringify(input)).toBe(before);
  });
}

test("Fable grouping preserves signed and opaque thinking blocks byte-for-byte", async () => {
  const assistant = Message.assistant([
    { type: "reasoning", text: "", encrypted: "exact-empty-signature" },
    { type: "reasoning", text: "inspect", encrypted: "exact-text-signature" },
    {
      type: "reasoning",
      text: "",
      providerMetadata: { anthropic: { redactedData: "opaque+/==" } },
    },
    ...batch("A", "B").content,
  ]);
  const { body } = await prepare(
    conversation(assistant, reply("A"), reply("B")),
  );
  expect(body).toMatchObject({
    messages: [
      { role: "user" },
      {
        role: "assistant",
        content: [
          {
            type: "thinking",
            thinking: "",
            signature: "exact-empty-signature",
          },
          {
            type: "thinking",
            thinking: "inspect",
            signature: "exact-text-signature",
          },
          { type: "redacted_thinking", data: "opaque+/==" },
          { type: "tool_use", id: "A" },
          { type: "tool_use", id: "B" },
        ],
      },
      { role: "user", content: [{ tool_use_id: "A" }, { tool_use_id: "B" }] },
    ],
  });
});

test("Fable does not demand local results for provider-executed tools", async () => {
  const assistant = Message.assistant([
    {
      type: "tool-call",
      id: "server-A",
      name: "web_search",
      input: { query: "test" },
      providerExecuted: true,
    },
    {
      type: "tool-result",
      id: "server-A",
      name: "web_search",
      result: { type: "json", value: { hits: [] } },
      providerExecuted: true,
    },
  ]);
  const { body } = await prepare(conversation(assistant));
  expect(body).toMatchObject({
    messages: [
      { role: "user" },
      {
        role: "assistant",
        content: [
          { type: "server_tool_use", id: "server-A" },
          { type: "web_search_tool_result", tool_use_id: "server-A" },
        ],
      },
    ],
  });
});

test("Astra uses stateless Responses tools without unsupported sampling parameters", async () => {
  const input = request("gpt-6-astra");
  expect(String(input.model.route.protocol)).toBe("openai-responses");
  const { body } = await prepare(input);
  expect(body).toMatchObject({
    model: "gpt-6-astra",
    tool_choice: "auto",
    max_output_tokens: 8192,
    store: false,
    include: ["reasoning.encrypted_content"],
    reasoning: { effort: "max" },
  });
  for (const key of [
    "temperature",
    "top_p",
    "seed",
    "logprobs",
    "top_logprobs",
  ])
    expect(body).not.toHaveProperty(key);
});

test("Fable uses Messages with adaptive thinking and automatic tool choice", async () => {
  const input = request("claude-fable-5-1");
  expect(String(input.model.route.protocol)).toBe("anthropic-messages");
  const { body } = await prepare(input);
  expect(body).toMatchObject({
    model: "claude-fable-5-1",
    tool_choice: { type: "auto" },
    max_tokens: 8192,
    thinking: { type: "adaptive" },
    output_config: { effort: "max" },
  });
  for (const key of ["temperature", "top_p", "top_k", "seed"])
    expect(body).not.toHaveProperty(key);
});

test("Qwen uses total completion tokens without sampling controls", async () => {
  const input = request("qwen3.8-max");
  const { body } = await prepare(input);
  expect(body).toMatchObject({
    model: "qwen3.8-max",
    max_completion_tokens: 8192,
  });
  expect(remainingGeneration(input, 1234)).toEqual({ maxTokens: 1234 });
});

test("Fable retains separate signed thinking blocks, including empty thinking", async () => {
  const input = request("claude-fable-5-1");
  const response = LLMResponse.fromEvents([
    LLMEvent.reasoningStart({ id: "r0" }),
    LLMEvent.reasoningEnd({
      id: "r0",
      providerMetadata: { anthropic: { signature: "sig-empty" } },
    }),
    LLMEvent.reasoningStart({ id: "r1" }),
    LLMEvent.reasoningDelta({ id: "r1", text: "inspect" }),
    LLMEvent.reasoningEnd({
      id: "r1",
      providerMetadata: { anthropic: { signature: "sig-text" } },
    }),
    LLMEvent.toolCall({
      id: "call-query",
      name: "query_company",
      input: { sql: "SELECT 1" },
    }),
    LLMEvent.finish({ reason: "tool-calls", usage: { outputTokens: 50 } }),
  ]);
  expect(response).toBeDefined();
  const next = LLMRequest.update(input, {
    messages: [
      ...input.messages,
      response!.message,
      Message.tool({
        id: "call-query",
        name: "query_company",
        result: { ok: true },
      }),
    ],
    generation: remainingGeneration(input, 8142),
  });
  expect(next.system).toEqual(input.system);
  expect(next.tools).toEqual(input.tools);
  const { body } = await prepare(next);
  expect(body).toMatchObject({
    messages: [
      {
        role: "user",
        content: [{ type: "text", text: "Inspect the company." }],
      },
      {
        role: "assistant",
        content: [
          { type: "thinking", thinking: "", signature: "sig-empty" },
          { type: "thinking", thinking: "inspect", signature: "sig-text" },
          {
            type: "tool_use",
            id: "call-query",
            name: "query_company",
            input: { sql: "SELECT 1" },
          },
        ],
      },
      {
        role: "user",
        content: [{ type: "tool_result", tool_use_id: "call-query" }],
      },
    ],
    tool_choice: { type: "auto" },
    max_tokens: 8142,
  });
});

test("Astra replays distinct opaque reasoning items and the matching tool result", async () => {
  const input = request("gpt-6-astra");
  const response = LLMResponse.fromEvents([
    ...["rs_1", "rs_2"].flatMap((id) => [
      LLMEvent.reasoningStart({ id }),
      LLMEvent.reasoningEnd({
        id,
        providerMetadata: {
          openai: { itemId: id, reasoningEncryptedContent: `opaque-${id}` },
        },
      }),
    ]),
    LLMEvent.toolCall({
      id: "call-query",
      name: "query_company",
      input: { sql: "SELECT 1" },
    }),
    LLMEvent.finish({ reason: "tool-calls", usage: { outputTokens: 25 } }),
  ]);
  const next = LLMRequest.update(input, {
    messages: [
      ...input.messages,
      response!.message,
      Message.tool({
        id: "call-query",
        name: "query_company",
        result: { ok: true },
      }),
    ],
    generation: remainingGeneration(input, 8167),
  });
  const { body } = await prepare(next);
  expect(body).toMatchObject({
    input: expect.arrayContaining([
      { type: "reasoning", summary: [], encrypted_content: "opaque-rs_1" },
      { type: "reasoning", summary: [], encrypted_content: "opaque-rs_2" },
      expect.objectContaining({
        type: "function_call_output",
        call_id: "call-query",
      }),
    ]),
    max_output_tokens: 8167,
    store: false,
  });
  expect(body).not.toHaveProperty("temperature");
});

test("Fable SSE preserves empty thinking and assembles fragmented signatures", async () => {
  const input = request("claude-fable-5-1");
  const { stream } = fableProtocol;
  let state = stream.initial(input);
  const events: LLMEvent[] = [];
  const frames = [
    {
      type: "content_block_start",
      index: 0,
      content_block: { type: "thinking", thinking: "" },
    },
    {
      type: "content_block_delta",
      index: 0,
      delta: { type: "signature_delta", signature: "first-" },
    },
    {
      type: "content_block_delta",
      index: 0,
      delta: { type: "signature_delta", signature: "signature" },
    },
    { type: "content_block_stop", index: 0 },
    {
      type: "content_block_start",
      index: 1,
      content_block: {
        type: "thinking",
        thinking: "inspect",
        signature: "second-signature",
      },
    },
    { type: "content_block_stop", index: 1 },
    {
      type: "message_delta",
      delta: { stop_reason: "end_turn" },
      usage: { input_tokens: 10, output_tokens: 20 },
    },
  ];
  for (const frame of frames) {
    const [next, emitted] = await Effect.runPromise(
      stream.step(state, JSON.stringify(frame)),
    );
    state = next;
    events.push(...emitted);
  }
  const response = LLMResponse.fromEvents(events)!;
  expect(response.message.content).toHaveLength(2);
  const { body } = await prepare(
    LLMRequest.update(input, {
      messages: [...input.messages, response.message],
    }),
  );
  expect(body).toMatchObject({
    messages: [
      expect.anything(),
      {
        role: "assistant",
        content: [
          { type: "thinking", thinking: "", signature: "first-signature" },
          {
            type: "thinking",
            thinking: "inspect",
            signature: "second-signature",
          },
        ],
      },
    ],
  });
  expect(response.usage?.outputTokens).toBe(20);
});

test("Fable preserves opaque thinking exactly and rejects missing opaque data", async () => {
  const { stream } = fableProtocol;
  const input = request("claude-fable-5-1");
  const [state, events] = await Effect.runPromise(
    stream.step(
      stream.initial(input),
      JSON.stringify({
        type: "content_block_start",
        index: 0,
        content_block: {
          type: "redacted_thinking",
          data: "opaque-provider-data",
        },
      }),
    ),
  );
  const [, finish] = await Effect.runPromise(
    stream.step(
      state,
      JSON.stringify({
        type: "message_delta",
        delta: { stop_reason: "end_turn" },
        usage: { output_tokens: 10 },
      }),
    ),
  );
  const response = LLMResponse.fromEvents([...events, ...finish])!;
  const { body } = await prepare(
    LLMRequest.update(input, {
      messages: [...input.messages, response.message],
    }),
  );
  expect(body).toMatchObject({
    messages: [
      expect.anything(),
      {
        role: "assistant",
        content: [{ type: "redacted_thinking", data: "opaque-provider-data" }],
      },
    ],
  });
  await expect(
    Effect.runPromise(
      stream.step(
        stream.initial(input),
        JSON.stringify({
          type: "content_block_start",
          index: 0,
          content_block: { type: "redacted_thinking" },
        }),
      ),
    ),
  ).rejects.toThrow("Invalid Fable streaming event");
});
