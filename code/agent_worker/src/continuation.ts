import { createHash } from "node:crypto";

import { type LLMResponse, Message } from "@opencode-ai/llm";

import type { TraceRecorder } from "./trace";

/** Exclude only a confirmed, unusable truncated reasoning tail from outbound history. */
export class TruncatedContinuation {
  static readonly feedback =
    "The provider reached its single-response output limit. No calls from this response were executed. Continue the same decision, retaining earlier staged changes, and re-emit complete tool calls before sealing alone.";

  constructor(private readonly trace: TraceRecorder) {}

  after(response: LLMResponse): ReadonlyArray<Message> {
    return this.repair([
      response.message,
      ...response.toolCalls.filter((call) => !call.providerExecuted).map((call) =>
        Message.tool({
          id: call.id,
          name: call.name,
          result: { type: "error", value: TruncatedContinuation.feedback },
        }),
      ),
      Message.user(TruncatedContinuation.feedback),
    ], "provider-response");
  }

  restore(messages: ReadonlyArray<Message>): ReadonlyArray<Message> {
    return this.repair(messages, "checkpoint");
  }

  private repair(
    messages: ReadonlyArray<Message>,
    source: "provider-response" | "checkpoint",
  ): ReadonlyArray<Message> {
    const index = messages.findLastIndex((message) => message.role === "assistant");
    const message = messages[index];
    const notice = messages[index + 1];
    if (
      !message?.content.length ||
      !message.content.every((part) => part.type === "reasoning") ||
      notice?.role !== "user" ||
      notice.content.length !== 1 ||
      notice.content[0]?.type !== "text" ||
      notice.content[0].text !== TruncatedContinuation.feedback ||
      !messages.slice(index + 1).every((item) => item.role === "user")
    ) return messages;

    this.trace.record("truncated-continuation-repaired", {
      source,
      removed_message_index: index,
      removed_message_sha256: createHash("sha256")
        .update(JSON.stringify(message)).digest("hex"),
      reason: "length_reasoning_only",
      draft_preserved: true,
      usage_preserved: true,
    });
    return messages.filter((_, position) => position !== index);
  }
}
