import { expect, test } from "bun:test";

import { LLM, LLMClient, type LLMEvent, type Model } from "@opencode-ai/llm";
import * as Anthropic from "@opencode-ai/llm/providers/anthropic";
import * as OpenAI from "@opencode-ai/llm/providers/openai";
import * as Chat from "@opencode-ai/llm/providers/openai-compatible";
import { RequestExecutor } from "@opencode-ai/llm/route";
import { Effect, Exit, Layer, Stream } from "effect";

import { describeFailure } from "./failure";
import { type ModelIdentityObservation, withIdentity } from "./identity";

type Wire = "chat" | "responses" | "messages";
const native = (wire: Wire, baseURL: string): Model => {
  const config = { baseURL, apiKey: "never-log-test-secret" };
  if (wire === "responses") return OpenAI.configure(config).responses("requested-model");
  if (wire === "messages") return Anthropic.configure(config).model("requested-model");
  return Chat.configure({ ...config, provider: "newapi" }).model("requested-model");
};

const frames = (wire: Wire, model?: string): object[] => {
  if (wire === "responses") return [
    { type: "response.created", response: { id: "r", model, reasoning: { effort: "max" } } },
    { type: "response.completed", response: { id: "r", model, status: "completed", usage: { input_tokens: 1, output_tokens: 1 } } },
  ];
  if (wire === "messages") return [
    { type: "message_start", message: { id: "m", model, usage: { input_tokens: 1, output_tokens: 0 } } },
    { type: "message_delta", delta: { stop_reason: "end_turn" }, usage: { output_tokens: 1 } },
    { type: "message_stop" },
  ];
  return [
    { model, choices: [{ delta: { content: "ok" }, finish_reason: null }] },
    { model, choices: [{ delta: {}, finish_reason: "stop" }], usage: { prompt_tokens: 1, completion_tokens: 1 } },
  ];
};

async function run(
  wire: Wire,
  payload: object[],
  expected: string[] = ["requested-model"],
  status = 200,
) {
  const observations: ModelIdentityObservation[] = [];
  const events: LLMEvent[] = [];
  const server = Bun.serve({
    hostname: "127.0.0.1", port: 0,
    fetch: () => status === 200
      ? new Response(payload.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""), {
        headers: { "content-type": "text/event-stream" },
      })
      : Response.json({ error: { message: "provider unavailable" } }, { status }),
  });
  try {
    const model = withIdentity(native(wire, server.url.origin + "/v1"), expected,
      (observation) => observations.push(observation));
    const result = await Effect.runPromise(Effect.exit(
      LLMClient.stream(LLM.request({ model, prompt: "Do not log the prompt" })).pipe(
        Stream.tap((event) => Effect.sync(() => { events.push(event); })),
        Stream.runCollect,
        Effect.provide(LLMClient.layer.pipe(Layer.provide(RequestExecutor.fetchLayer))),
      ),
    ));
    return {
      observations, events,
      failure: Exit.isFailure(result) ? describeFailure(result.cause) : undefined,
    };
  } finally { server.stop(true); }
}

test.each(["chat", "responses", "messages"] as const)("%s validates identity before native schemas erase it", async (wire) => {
  const result = await run(wire, frames(wire, "requested-model"));
  expect(result.failure).toBeUndefined();
  expect(result.observations).toHaveLength(1);
  expect(result.observations[0]).toMatchObject({
    requested_model: "requested-model", served_model: "requested-model", status: "matched",
    echoed_effort: wire === "responses" ? "max" : null,
  });
});

test.each(["chat", "responses", "messages"] as const)("%s rejects a wrong serving identity before any model event", async (wire) => {
  const result = await run(wire, frames(wire, "different-model"));
  expect(result.events).toHaveLength(0);
  expect(result.failure).toMatchObject({ category: "identity", retryable: false, reason: "ServingModelMismatch" });
  expect(result.observations[0]).toMatchObject({ served_model: "different-model", status: "mismatch" });
});

test.each(["chat", "responses", "messages"] as const)("%s cannot complete without the required serving identity", async (wire) => {
  const result = await run(wire, frames(wire));
  expect(result.failure).toMatchObject({ category: "identity", retryable: false, reason: "ServingModelMissing" });
  expect(result.events.some((event) => event.type === "finish")).toBe(false);
});

test("response.completed identity is checked even after an initially matching response.created", async () => {
  const payload = frames("responses", "requested-model");
  payload[1] = frames("responses", "different-model")[1]!;
  const result = await run("responses", payload);
  expect(result.failure?.reason).toBe("ServingModelMismatch");
  expect(result.events.some((event) => event.type === "finish")).toBe(false);
});

test("explicit accepted version aliases are allowed without equating arbitrary labels", async () => {
  const result = await run("chat", frames("chat", "requested-model-20260911"), ["requested-model", "requested-model-20260911"]);
  expect(result.failure).toBeUndefined();
  expect(result.observations[0]?.served_model).toBe("requested-model-20260911");
});

test("generic fixtures may explicitly opt out without inventing an identity", async () => {
  const result = await run("chat", frames("chat"), []);
  expect(result.failure).toBeUndefined();
  expect(result.observations).toHaveLength(0);
});

test("an HTTP 402 is retained instead of replaced with a missing-identity error", async () => {
  const result = await run("chat", [], ["requested-model"], 402);
  expect(result.failure).toMatchObject({ http_status: 402 });
  expect(result.failure?.reason).not.toBe("ServingModelMissing");
  expect(result.observations).toHaveLength(0);
});

test("a provider error event is not masked by missing identity", async () => {
  const result = await run("responses", [
    { type: "response.failed", response: { id: "r", error: { code: "server_error", message: "provider unavailable" } } },
  ]);
  expect(result.failure?.category).not.toBe("identity");
  expect(result.events.some((event) => event.type === "provider-error")).toBe(true);
  expect(result.observations).toHaveLength(0);
});

test("arbitrary upstream text is never copied into identity diagnostics", async () => {
  const result = await run("chat", frames("chat", "sk-private-token"));
  expect(result.failure?.reason).toBe("ServingModelMismatch");
  expect(JSON.stringify(result)).not.toContain("sk-private-token");
  expect(JSON.stringify(result)).not.toContain("never-log-test-secret");
  expect(result.observations[0]?.served_model).toBe("<invalid-model-id>");
});
