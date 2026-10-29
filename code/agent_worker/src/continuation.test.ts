import { expect, test } from "bun:test";

import { LLMEvent, LLMResponse, Message } from "@opencode-ai/llm";

import { TruncatedContinuation } from "./continuation";
import { TraceRecorder } from "./trace";

class RecordingTrace extends TraceRecorder {
  readonly repairs: unknown[] = [];

  constructor() { super("offline"); }

  override record(kind: string, payload: unknown): void {
    if (kind === "truncated-continuation-repaired") this.repairs.push(payload);
  }
}

const reasoning = Message.assistant({
  type: "reasoning", text: "unfinished private reasoning",
  providerMetadata: { anthropic: { signature: "OPAQUE_SIGNATURE" } },
});
const notice = Message.user(TruncatedContinuation.feedback);

test("repairs only the marked reasoning-only tail, once, preserving evidence and finalization", () => {
  const trace = new RecordingTrace();
  const repair = new TruncatedContinuation(trace);
  const evidence = Message.tool({ id: "query", name: "query_company", result: "EXACT_EVIDENCE" });
  const closing = Message.user("FINALIZATION: query_company is disabled.");
  const messages = [Message.user("briefing"), evidence, reasoning, notice, closing];
  const original = JSON.stringify(messages);
  const restored = repair.restore(messages);
  expect(restored).toEqual([messages[0], evidence, notice, closing]);
  expect(JSON.stringify(messages)).toBe(original);
  expect(trace.repairs).toHaveLength(1);
  expect(trace.repairs[0]).toMatchObject({
    source: "checkpoint", removed_message_index: 2,
    reason: "length_reasoning_only", draft_preserved: true, usage_preserved: true,
  });
  expect(repair.restore(restored)).toBe(restored);
  expect(trace.repairs).toHaveLength(1);
});

test("unmarked, empty, textual, tool-bearing and previously completed reasoning stays intact", () => {
  const trace = new RecordingTrace();
  const repair = new TruncatedContinuation(trace);
  const call = { type: "tool-call" as const, id: "query", name: "query_company", input: { sql: "SELECT 1" } };
  const histories = [
    [reasoning, Message.user("No tool was called.")],
    [Message.assistant([]), notice],
    [Message.assistant([...reasoning.content, Message.text("Useful reply")]), notice],
    [Message.assistant([...reasoning.content, call]), notice],
    [reasoning, notice, Message.assistant("Successful continuation"), Message.user("Continue")],
  ];
  for (const messages of histories) expect(repair.restore(messages)).toBe(messages);
  expect(trace.repairs).toEqual([]);
});

test("a truncated tool turn keeps signed reasoning and matching rejection receipts", () => {
  const trace = new RecordingTrace();
  const response = LLMResponse.fromEvents([
    LLMEvent.reasoningStart({ id: "reason" }),
    LLMEvent.reasoningEnd({ id: "reason", providerMetadata: { anthropic: { signature: "EXACT_SIGNATURE" } } }),
    LLMEvent.toolCall({ id: "seal", name: "seal_decision", input: {} }),
    LLMEvent.finish({ reason: "length", usage: { outputTokens: 10 } }),
  ])!;
  const messages = new TruncatedContinuation(trace).after(response);
  expect(messages).toHaveLength(3);
  expect(messages[0]).toBe(response.message);
  expect(messages[1]).toMatchObject({ role: "tool", content: [{
    type: "tool-result", id: "seal", name: "seal_decision",
    result: { type: "error", value: TruncatedContinuation.feedback },
  }] });
  expect(messages[2]).toEqual(notice);
  expect(trace.repairs).toEqual([]);
});
