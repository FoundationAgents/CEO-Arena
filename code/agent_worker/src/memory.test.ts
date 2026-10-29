import { expect, test } from "bun:test";
import { linkSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import path from "node:path";

import {
  LLMClient,
  LLMEvent,
  LLMRequest,
  LLMResponse,
  Message,
  toDefinitions,
} from "@opencode-ai/llm";
import { RequestExecutor } from "@opencode-ai/llm/route";
import { Effect, Layer, Stream } from "effect";

import { ConversationJournal } from "./conversation";
import { TruncatedContinuation } from "./continuation";
import { AgentToolGateway } from "./draft";
import { ProviderFault } from "./failure";
import { resolveInference } from "./inference";
import { CompanyWorkspace, MEMORY_CHAR_LIMIT, WorkspaceError } from "./memory";
import type { DecisionRequest } from "./protocol";
import { NewApiModel, type ProviderConfig } from "./provider";
import { runDecision } from "./runner";
import { decisionFixture } from "./testing";
import { createTools } from "./tools";
import { TraceRecorder } from "./trace";
import { EMPTY_USAGE } from "./usage";

const call = (name: string, input: unknown = {}, id = name) =>
  LLMEvent.toolCall({ id, name, input });
const finish = () =>
  LLMEvent.finish({ reason: "tool-calls", usage: { outputTokens: 10 } });
const seal = [call("seal_decision"), finish()];
const provider = {
  apiKey: "offline",
  baseURL: "http://127.0.0.1:1/v1",
  model: "offline",
};

async function simulate(
  workspace: CompanyWorkspace,
  turns: readonly (readonly LLMEvent[])[],
  input = decisionFixture(),
  config: ProviderConfig = provider,
  requests: LLMRequest[] = [],
) {
  const client = Layer.succeed(LLMClient.Service, {
    prepare: LLMClient.prepare,
    stream(request: LLMRequest) {
      requests.push(request);
      const response = turns[requests.length - 1];
      return response
        ? Stream.fromIterable(response)
        : Stream.die(
            new ProviderFault({
              category: "http",
              http_status: 401,
              retryable: false,
              message: "offline interruption",
            }),
          );
    },
    generate: () => Effect.die("No auxiliary generation is permitted"),
  });
  const result = await Effect.runPromise(
    runDecision(
      input,
      config,
      {
        query: async () => ({
          columns: ["value"],
          rows: [["TRANSIENT_SQL_EVIDENCE"]],
          truncated: false,
        }),
        validate: async () => {},
      },
      new TraceRecorder("offline"),
      undefined,
      { workspace },
    ).pipe(Effect.provide(client)),
  );
  return { result, requests };
}

const nextWeek = (input: DecisionRequest): DecisionRequest => ({
  ...input,
  request: {
    ...input.request,
    world_version: input.request.world_version + 1,
    observation: {
      ...input.request.observation,
      world_version: input.request.world_version + 1,
      day: input.request.observation.day + 7,
      days_remaining: input.request.observation.days_remaining - 7,
    },
  },
});

test("memory auto-load uses 40,000 Unicode characters; the full file remains readable", () => {
  const workspace = CompanyWorkspace.ephemeral("test", "own");
  try {
    const content = "😀".repeat(MEMORY_CHAR_LIMIT + 1) + "\nTAIL";
    workspace.write("MEMORY.md", content, "write-once");
    expect(
      Array.from(workspace.memory()).filter((c) => c === "😀"),
    ).toHaveLength(MEMORY_CHAR_LIMIT);
    expect(workspace.memory()).toContain("MEMORY.md TRUNCATED");
    expect(workspace.memory()).not.toContain("TAIL");
    expect(workspace.read("MEMORY.md", 2, 1)).toContain("TAIL");
    expect(readFileSync(path.join(workspace.files, "MEMORY.md"), "utf8")).toBe(
      content,
    );
  } finally {
    workspace.close();
  }
});

test("file writes persist immediately and edit receipts prevent duplicate replacement", () => {
  const workspace = CompanyWorkspace.ephemeral("test", "own");
  try {
    workspace.write("notes/plan.txt", "x", "write");
    const first = workspace.edit("notes/plan.txt", "x", "xx", "edit");
    expect(workspace.edit("notes/plan.txt", "x", "xx", "edit")).toBe(first);
    expect(workspace.read("notes/plan.txt")).toContain("xx");
    expect(() =>
      workspace.edit("notes/plan.txt", "x", "z", "different-edit"),
    ).toThrow("exactly once");
    expect(workspace.glob("*")).toContain("notes");
    expect(workspace.glob("**/*.txt")).toContain("notes/plan.txt");
    expect(workspace.search("xx", "notes")).toContain("notes/plan.txt:1: xx");
    expect(() => workspace.search("[")).toThrow(WorkspaceError);
  } finally {
    workspace.close();
  }
});

for (const relative of [
  "../runtime/conversation.json",
  "../../key",
  "C:\\key",
  "/key",
  "\\\\server\\key",
  "MEMORY.md:secret",
  "NUL",
  "notes/../key",
  "notes. /key",
]) {
  test(`private file tools reject escape or device path: ${relative}`, () => {
    const workspace = CompanyWorkspace.ephemeral("test", "own");
    try {
      expect(() => workspace.read(relative)).toThrow(WorkspaceError);
      expect(() => workspace.write(relative, "forbidden", "bad")).toThrow(
        WorkspaceError,
      );
    } finally {
      workspace.close();
    }
  });
}

test("company/run directories are separate and junctions or hard links cannot expose files", () => {
  const own = CompanyWorkspace.ephemeral("run", "own");
  try {
    const root = path.dirname(path.dirname(own.files));
    const rival = new CompanyWorkspace(root, "run", "rival");
    const otherRun = new CompanyWorkspace(root, "another-run", "own");
    rival.write("MEMORY.md", "PRIVATE_RIVAL_MEMORY", "rival-write");
    expect(own.memory()).toBe("");
    expect(otherRun.memory()).toBe("");
    symlinkSync(rival.files, path.join(own.files, "linked"), "junction");
    expect(() => own.read("linked/MEMORY.md")).toThrow(WorkspaceError);
    expect(() => own.write("linked/MEMORY.md", "overwrite", "bad")).toThrow(
      WorkspaceError,
    );
    linkSync(
      path.join(rival.files, "MEMORY.md"),
      path.join(own.files, "hardlink.txt"),
    );
    expect(() => own.read("hardlink.txt")).toThrow(WorkspaceError);
    expect(readFileSync(path.join(rival.files, "MEMORY.md"), "utf8")).toBe(
      "PRIVATE_RIVAL_MEMORY",
    );
  } finally {
    own.close();
  }
});

test("new weeks load MEMORY.md, not old conversations or all files; details are read on demand", async () => {
  const input = decisionFixture();
  const workspace = CompanyWorkspace.ephemeral(
    input.request.run_id,
    input.request.company_id,
  );
  try {
    const first = await simulate(
      workspace,
      [
        [
          call("query_company", { sql: "SELECT value" }),
          call("write_file", {
            path: "MEMORY.md",
            content: "KEEP_STRATEGY_FACT",
          }),
          call(
            "write_file",
            { path: "notes/detail.md", content: "DETAIL_ONLY_ON_DEMAND" },
            "detail",
          ),
          finish(),
        ],
        seal,
      ],
      input,
    );
    expect(first.result.status).toBe("sealed");
    const second = await simulate(
      workspace,
      [[call("read_file", { path: "notes/detail.md" }), finish()], seal],
      nextWeek(input),
    );
    expect(second.result.status).toBe("sealed");
    expect(JSON.stringify(second.requests[0]?.system)).toContain(
      "KEEP_STRATEGY_FACT",
    );
    expect(JSON.stringify(second.requests[0])).not.toContain(
      "TRANSIENT_SQL_EVIDENCE",
    );
    expect(JSON.stringify(second.requests[0])).not.toContain(
      "DETAIL_ONLY_ON_DEMAND",
    );
    expect(JSON.stringify(second.requests[1])).toContain(
      "DETAIL_ONLY_ON_DEMAND",
    );
    expect(second.requests).toHaveLength(2);
  } finally {
    workspace.close();
  }
});

test("reset and exhaustion discard economic drafts but do not roll back private memory", async () => {
  const input = decisionFixture();
  const workspace = CompanyWorkspace.ephemeral(
    input.request.run_id,
    input.request.company_id,
  );
  try {
    const exhausted = await simulate(
      workspace,
      [
        [
          call("set_product", { product_id: "basic", price_cents: 1900 }),
          call("write_file", {
            path: "MEMORY.md",
            content: "Unexecuted proposal; verify actual settlement.",
          }),
          call("reset_decision"),
          finish(),
        ],
      ],
      {
        ...input,
        request: {
          ...input.request,
          budget: { ...input.request.budget, max_provider_turns: 1 },
        },
      },
    );
    expect(exhausted.result.status).toBe("budget_exhausted");
    expect(exhausted.result.decision).toBeUndefined();
    expect(workspace.memory()).toContain("Unexecuted proposal");
    const next = await simulate(workspace, [seal], nextWeek(input));
    expect(next.result.status).toBe("sealed");
    expect(next.result.decision?.products).toEqual([]);
  } finally {
    workspace.close();
  }
});

test("same-window recovery restores raw evidence and subtracts already reported usage", async () => {
  const input = decisionFixture();
  const workspace = CompanyWorkspace.ephemeral(
    input.request.run_id,
    input.request.company_id,
  );
  try {
    const first = await simulate(
      workspace,
      [[call("query_company", { sql: "SELECT value" }), finish()]],
      input,
    );
    expect(first.result.status).toBe("provider_error");
    const resumed = await simulate(workspace, [seal], {
      ...input,
      request: {
        ...input.request,
        prior_usage: first.result.usage,
        prior_diagnostics: first.result.diagnostics,
      },
    });
    expect(resumed.result.status).toBe("sealed");
    expect(resumed.requests).toHaveLength(1);
    expect(JSON.stringify(resumed.requests[0])).toContain(
      "TRANSIENT_SQL_EVIDENCE",
    );
    expect(resumed.result.usage.provider_turns).toBe(1);
    expect(resumed.result.usage.output_tokens).toBe(10);
    const cached = await simulate(workspace, [], input);
    expect(cached.result.status).toBe("sealed");
    expect(cached.requests).toHaveLength(0);
    expect(cached.result.usage.provider_turns).toBe(2);
    const exhaustedPrior = {
      ...cached.result.usage,
      provider_turns: input.request.budget.max_provider_turns,
      finalization_turns: 4,
      output_tokens: input.request.budget.max_output_tokens!,
      latency_ms: input.request.budget.timeout_seconds! * 1000,
    };
    const expired = await simulate(workspace, [], {
      ...input,
      request: { ...input.request, prior_usage: exhaustedPrior },
    });
    expect(expired.requests).toHaveLength(0);
    expect(expired.result.status).toBe("sealed");
    expect(expired.result.decision).toEqual(cached.result.decision);
    expect(expired.result.usage).toMatchObject({
      provider_turns: 0, finalization_turns: 0, output_tokens: 0,
    });
  } finally {
    workspace.close();
  }
});

test("legacy truncated checkpoints resume the existing draft without rebuying completed replies", async () => {
  const input = decisionFixture();
  const workspace = CompanyWorkspace.ephemeral(input.request.run_id, input.request.company_id);
  const truncated = [
    LLMEvent.reasoningStart({ id: "reason" }),
    LLMEvent.reasoningDelta({ id: "reason", text: "TRUNCATED_REASONING" }),
    LLMEvent.reasoningEnd({ id: "reason" }),
    LLMEvent.finish({ reason: "length", usage: { outputTokens: 20 } }),
  ];
  try {
    const first = await simulate(workspace, [
      [
        call("query_company", { sql: "SELECT value" }),
        call("set_product", { product_id: "basic", price_cents: 1800 }),
        call("write_file", { path: "MEMORY.md", content: "Preserve this paid decision evidence." }),
        finish(),
      ],
      truncated,
    ], input);
    expect(first.result.status).toBe("provider_error");
    const checkpoint = path.join(workspace.internal, "conversation.json");
    const legacy = JSON.parse(readFileSync(checkpoint, "utf8"));
    const index = legacy.messages.findIndex((message: Message) =>
      message.role === "user" && message.content[0]?.type === "text" &&
      message.content[0].text === TruncatedContinuation.feedback);
    expect(index).toBeGreaterThan(0);
    legacy.messages.splice(index, 0, LLMResponse.fromEvents(truncated)!.message);
    writeFileSync(checkpoint, JSON.stringify(legacy));
    const resumed = await simulate(workspace, [seal], {
      ...input,
      request: { ...input.request, prior_usage: first.result.usage, prior_diagnostics: first.result.diagnostics },
    });
    expect(resumed.requests).toHaveLength(1);
    expect(resumed.result.status).toBe("sealed");
    expect(resumed.result.decision?.products[0]?.price_cents).toBe(1800);
    expect(resumed.result.usage).toMatchObject({ provider_turns: 1, output_tokens: 10 });
    const outgoing = JSON.stringify(resumed.requests[0]?.messages);
    expect(outgoing).toContain("TRANSIENT_SQL_EVIDENCE");
    expect(outgoing).not.toContain("TRUNCATED_REASONING");
    expect(workspace.memory()).toContain("Preserve this paid decision evidence");
    const saved = JSON.parse(readFileSync(checkpoint, "utf8"));
    expect(saved.usage).toMatchObject({ provider_turns: 3, output_tokens: 40 });
    expect(saved.messages.some((message: Message) => message.content.some(
      (part) => part.type === "reasoning" && part.text === "TRUNCATED_REASONING",
    ))).toBe(false);
  } finally { workspace.close(); }
});

test.each([
  { max_output_tokens: 2048 },
  { effort: "high" as const },
])("same-window recovery rejects changed inference %j before buying another response", async (change) => {
  const input = decisionFixture();
  const workspace = CompanyWorkspace.ephemeral(input.request.run_id, input.request.company_id);
  try {
    const first = await simulate(workspace,
      [[call("query_company", { sql: "SELECT value" }), finish()]], input);
    expect(first.result.status).toBe("provider_error");
    const checkpointPath = path.join(workspace.internal, "conversation.json");
    const before = readFileSync(checkpointPath, "utf8");
    const requests: LLMRequest[] = [];
    await expect(simulate(workspace, [seal], input, {
      ...provider,
      inference: { ...resolveInference(provider.model), ...change },
    }, requests)).rejects.toThrow("differs from the frozen window");
    expect(requests).toHaveLength(0);
    expect(readFileSync(checkpointPath, "utf8")).toBe(before);
  } finally { workspace.close(); }
});

test("invalid filesystem requests are model tool errors, not permanent worker failures", async () => {
  const input = decisionFixture();
  const workspace = CompanyWorkspace.ephemeral(
    input.request.run_id,
    input.request.company_id,
  );
  try {
    const { result } = await simulate(
      workspace,
      [
        [
          call("write_file", { path: "x".repeat(300), content: "bad path" }),
          finish(),
        ],
        seal,
      ],
      input,
    );
    expect(result.status).toBe("sealed");
    expect(result.diagnostics.tool_errors).toBe(1);
    expect(result.diagnostics.recovered).toBe(true);
  } finally {
    workspace.close();
  }
});

test("soft deadline blocks pending queries without dropping other tools or native message pairs", async () => {
  const input = decisionFixture();
  let queries = 0;
  let replies = 0;
  const client = Layer.succeed(LLMClient.Service, {
    prepare: LLMClient.prepare,
    stream: () =>
      Stream.fromIterable(
        ++replies === 1
          ? [
              call("query_company", { sql: "SELECT value" }),
              call("set_product", { product_id: "basic", price_cents: 1500 }),
              finish(),
            ]
          : seal,
      ),
    generate: () => Effect.die("stream only"),
  });
  const result = await Effect.runPromise(
    runDecision(
      {
        ...input,
        request: {
          ...input.request,
          budget: {
            ...input.request.budget,
            max_provider_turns: 32,
            timeout_seconds: 4,
          },
        },
      },
      provider,
      {
        query: () => {
          queries++;
          return new Promise(() => {});
        },
        validate: async () => {},
      },
      new TraceRecorder("offline"),
    ).pipe(Effect.provide(client)),
  );
  expect(result.status).toBe("sealed");
  expect(result.decision?.products[0]?.price_cents).toBe(1500);
  expect(result.diagnostics.protocol_errors).toBe(0);
  expect(queries).toBe(1);
  expect(replies).toBe(2);
}, 10000);

for (const modelID of ["gpt-5.6-luna", "gpt-6-astra", "claude-fable-5-1"]) {
  test(`${modelID}: snapshots preserve the native wire body, including opaque reasoning`, async () => {
    const workspace = CompanyWorkspace.ephemeral("native", modelID);
    try {
      const fixture = decisionFixture();
      const draft = new AgentToolGateway(fixture.request);
      const definitions = toDefinitions(
        createTools(draft, {
          query: async () => ({ columns: [], rows: [], truncated: false }),
          validate: async () => {},
        }),
      );
      const base = new NewApiModel({ ...provider, model: modelID }).request({
        system: "SYSTEM",
        prompt: "Current week",
        tools: definitions,
      });
      const response = LLMResponse.fromEvents([
        LLMEvent.reasoningStart({ id: "reason" }),
        LLMEvent.reasoningEnd({
          id: "reason",
          providerMetadata: modelID.startsWith("claude")
            ? { anthropic: { signature: "EXACT_SIGNATURE" } }
            : {
                openai: {
                  itemId: "reason",
                  reasoningEncryptedContent: "EXACT_ENCRYPTED",
                },
              },
        }),
        call("query_company", { sql: "SELECT 1" }),
        finish(),
      ])!;
      const request = LLMRequest.update(base, {
        messages: [
          ...base.messages,
          response.message,
          Message.tool({
            id: "query_company",
            name: "query_company",
            result: { value: "EXACT_RESULT" },
          }),
        ],
      });
      const journal = new ConversationJournal(workspace, 1, "binding");
      journal.save({
        system: request.system,
        messages: request.messages,
        draft: draft.draft.preview(),
        usage: EMPTY_USAGE,
        diagnostics: { protocol_errors: 0, tool_errors: 0, recovered: false },
        closing: false,
      });
      const saved = journal.load()!;
      expect(saved.messages.every((message) => message instanceof Message)).toBe(true);
      expect(JSON.stringify(saved.messages)).toBe(
        JSON.stringify(request.messages),
      );
      const restored = LLMRequest.update(base, {
        system: saved.system,
        messages: saved.messages,
      });
      const prepare = (input: LLMRequest) =>
        Effect.runPromise(
          LLMClient.prepare(input).pipe(
            Effect.provide(
              LLMClient.layer.pipe(Layer.provide(RequestExecutor.fetchLayer)),
            ),
          ),
        );
      expect((await prepare(restored)).body).toEqual(
        (await prepare(request)).body,
      );
      const closing = LLMRequest.update(restored, {
        tools: restored.tools.filter((tool) => tool.name !== "query_company"),
      });
      expect(JSON.stringify(closing.messages)).toBe(
        JSON.stringify(request.messages),
      );
      await prepare(closing); // All three native serializers accept intact historical tool pairs.
      expect(() =>
        new ConversationJournal(workspace, 1, "different-binding").load(),
      ).toThrow("frozen window");
      expect(
        new ConversationJournal(workspace, 2, "new-week").load(),
      ).toBeUndefined();
      journal.save({
        ...saved,
        pending: {
          message: response.message,
          calls: response.toolCalls,
          results: [
            Message.tool({
              id: "wrong-id",
              name: "query_company",
              result: "wrong",
            }),
          ],
        },
      });
      expect(() => journal.load()).toThrow("tool receipts do not match");
      writeFileSync(
        path.join(workspace.internal, "conversation.json"),
        "broken",
      );
      expect(() => journal.load()).toThrow("refusing to reset");
    } finally {
      workspace.close();
    }
  });
}
