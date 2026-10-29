import type { LLMEvent, LLMRequest } from "@opencode-ai/llm";
import * as Anthropic from "@opencode-ai/llm/providers/anthropic";
import * as Messages from "@opencode-ai/llm/protocols/anthropic-messages";
import { ProviderShared } from "@opencode-ai/llm/protocols/shared";
import { Lifecycle } from "@opencode-ai/llm/protocols/utils/lifecycle";
import { Framing, Protocol, Route } from "@opencode-ai/llm/route";
import { Effect, Schema } from "effect";

import {
  type InferenceSettings,
  ReasoningEffort,
  resolveInference,
  responseTokenLimit,
} from "./inference";
import { recoverMessages } from "./recovery";

const upstream = Messages.protocol;
const RedactedThinking = Schema.Struct({
  type: Schema.Literal("redacted_thinking"),
  data: Schema.String,
});
const RedactedFrame = Protocol.jsonEvent(
  Schema.Struct({ content_block: RedactedThinking }),
);
type NativeBody = Messages.AnthropicMessagesBody;
type NativeMessage = NativeBody["messages"][number];
type FableMessage = Omit<NativeMessage, "content"> & {
  readonly content: ReadonlyArray<
    NativeMessage["content"][number] | typeof RedactedThinking.Type
  >;
};
const AdaptiveThinking = Schema.Struct({ type: Schema.Literal("adaptive") });
const OutputConfig = Schema.Struct({ effort: ReasoningEffort });
const strict = { onExcessProperty: "error" } as const;
const acceptsRedactedThinking = Schema.is(
  RedactedThinking.annotate({ parseOptions: strict }),
);
const acceptsOutputConfig = Schema.is(
  OutputConfig.annotate({ parseOptions: strict }),
);
const acceptsAdaptiveThinking = Schema.is(
  AdaptiveThinking.annotate({ parseOptions: strict }),
);
const acceptsNativeBody = Schema.is(
  upstream.body.schema.annotate({ parseOptions: strict }),
);
type FableBody = Omit<NativeBody, "messages" | "thinking"> & {
  readonly messages: ReadonlyArray<FableMessage>;
  readonly thinking?: NativeBody["thinking"] | typeof AdaptiveThinking.Type;
  readonly output_config?: typeof OutputConfig.Type;
};
type ParserState = ReturnType<typeof upstream.stream.initial> & {
  readonly signatures: ReadonlyMap<number, string>;
};

// Extend the upstream schema only for adaptive effort and opaque thinking.
// Opaque provider data is preserved exactly, never interpreted or summarized.
const Body = Schema.declare<FableBody>((value): value is FableBody => {
  if (!ProviderShared.isRecord(value) || !Array.isArray(value.messages))
    return false;
  const { output_config: output, ...native } = value;
  if (output !== undefined && !acceptsOutputConfig(output)) return false;
  const adaptive =
    ProviderShared.isRecord(native.thinking) &&
    native.thinking.type === "adaptive";
  if (adaptive && !acceptsAdaptiveThinking(native.thinking)) return false;
  const messages: unknown[] = [];
  for (const message of value.messages) {
    if (!ProviderShared.isRecord(message) || !Array.isArray(message.content))
      return false;
    const content: unknown[] = [];
    for (const block of message.content) {
      if (
        ProviderShared.isRecord(block) &&
        block.type === "redacted_thinking"
      ) {
        if (message.role !== "assistant" || !acceptsRedactedThinking(block))
          return false;
        content.push({ type: "thinking", thinking: "" });
      } else content.push(block);
    }
    messages.push({ ...message, content });
  }
  return acceptsNativeBody({
    ...native,
    messages,
    ...(adaptive ? { thinking: undefined } : {}),
  });
});

const restoreOpaqueBlocks = (
  body: NativeBody,
  request: LLMRequest,
): FableBody => {
  // Native lowering preserves assistant messages and their content order.
  const assistants = request.messages.filter(
    (message) => message.role === "assistant",
  );
  let index = 0;
  return {
    ...body,
    messages: body.messages.map((message): FableMessage => {
      if (message.role !== "assistant") return message;
      const source = assistants[index++]!;
      return {
        ...message,
        content: message.content.map((block, position) => {
          const part = source.content[position];
          const data =
            part?.type === "reasoning"
              ? part.providerMetadata?.anthropic?.redactedData
              : undefined;
          return typeof data === "string"
            ? { type: "redacted_thinking", data }
            : block;
        }),
      };
    }),
  };
};

/** Match one assistant tool batch to one adjacent user result message. */
class FableMessageNormalizer {
  private readonly messages: FableMessage[] = [];
  private readonly pending = new Set<string>();
  private resultIndex: number | undefined;

  static normalize(body: FableBody): FableBody {
    const normalizer = new FableMessageNormalizer();
    for (const message of body.messages) normalizer.append(message);
    normalizer.requireComplete();
    return { ...body, messages: normalizer.messages };
  }

  private append(message: FableMessage): void {
    const results = message.content.filter(
      (block) => block.type === "tool_result",
    );
    if (results.length) {
      if (message.role !== "user" || results.length !== message.content.length)
        throw new Error(
          "Fable tool history: results cannot cross ordinary message content",
        );
      for (const result of results) {
        if (!this.pending.delete(result.tool_use_id))
          throw new Error(
            "Fable tool history: unexpected or duplicate result " +
              result.tool_use_id,
          );
      }
      if (this.resultIndex === undefined) {
        this.resultIndex = this.messages.length;
        this.messages.push(message);
      } else {
        const previous = this.messages[this.resultIndex]!;
        this.messages[this.resultIndex] = {
          ...previous,
          content: [...previous.content, ...message.content],
        };
      }
      return;
    }

    this.requireComplete();
    this.resultIndex = undefined;
    if (message.role === "assistant") {
      for (const block of message.content) {
        if (block.type !== "tool_use") continue;
        if (this.pending.has(block.id))
          throw new Error("Fable tool history: duplicate call " + block.id);
        this.pending.add(block.id);
      }
    }
    this.messages.push(message);
  }

  private requireComplete(): void {
    if (this.pending.size)
      throw new Error(
        "Fable tool history: missing results for " +
          [...this.pending].join(", "),
      );
  }
}

const malformedEvent = () =>
  ProviderShared.eventError(
    "anthropic-messages",
    "Invalid Fable streaming event",
  );

/** Keep signed and opaque thinking intact without patching the pinned OpenCode checkout. */
export const fableProtocol = Protocol.make({
  ...upstream,
  body: {
    schema: Body,
    from: (request) =>
      upstream.body.from(request).pipe(
        Effect.flatMap((body) =>
          Effect.try({
            try: () =>
              FableMessageNormalizer.normalize(
                restoreOpaqueBlocks(body, request),
              ),
            catch: (error) =>
              ProviderShared.eventError(
                "anthropic-messages",
                error instanceof Error
                  ? error.message
                  : "Invalid Fable tool history",
              ),
          }),
        ),
      ),
  },
  stream: {
    event: Schema.String,
    initial: (request): ParserState => ({
      ...upstream.stream.initial(request),
      signatures: new Map(),
    }),
    step: (state: ParserState, frame: string) =>
      Effect.gen(function* () {
        const event = yield* Schema.decodeUnknownEffect(upstream.stream.event)(
          frame,
        ).pipe(Effect.mapError(malformedEvent));
        const index = event.index ?? 0;
        const id = `reasoning-${index}`;
        const events: LLMEvent[] = [];
        let lifecycle = state.lifecycle;
        const signatures = new Map(state.signatures);
        if (event.type === "content_block_start") {
          const block = event.content_block;
          if (block?.type === "redacted_thinking") {
            const redacted = yield* Schema.decodeUnknownEffect(RedactedFrame)(
              frame,
            ).pipe(Effect.mapError(malformedEvent));
            const metadata = {
              anthropic: { redactedData: redacted.content_block.data },
            };
            lifecycle = Lifecycle.reasoningStart(
              lifecycle,
              events,
              id,
              metadata,
            );
            lifecycle = Lifecycle.reasoningEnd(lifecycle, events, id, metadata);
            return [{ ...state, lifecycle }, events] as const;
          }
          if (block?.type === "thinking") {
            lifecycle = Lifecycle.reasoningStart(lifecycle, events, id);
            signatures.set(index, block.signature ?? "");
          }
        }
        if (
          event.type === "content_block_delta" &&
          event.delta?.type === "signature_delta"
        ) {
          signatures.set(
            index,
            (signatures.get(index) ?? "") + (event.delta.signature ?? ""),
          );
          return [{ ...state, signatures }, events] as const;
        }
        if (event.type === "content_block_stop" && signatures.has(index)) {
          const signature = signatures.get(index)!;
          lifecycle = Lifecycle.reasoningEnd(
            lifecycle,
            events,
            id,
            signature ? { anthropic: { signature } } : undefined,
          );
          signatures.delete(index);
        }
        const [next, emitted] = yield* upstream.stream.step(
          { ...state, lifecycle },
          event,
        );
        return [{ ...next, signatures }, [...events, ...emitted]] as const;
      }),
  },
});

export const fableModel = (
  config: Anthropic.Config,
  model: string,
  settings: InferenceSettings = resolveInference(model),
) => {
  const native = Anthropic.configure(config).model(model).route;
  const configured = Protocol.make({
    ...fableProtocol,
    body: {
      schema: Body,
      from: (request) =>
        fableProtocol.body.from(request).pipe(
          Effect.map(
            (body): FableBody => ({
              ...body,
              ...(settings.thinking === "adaptive"
                ? { thinking: { type: "adaptive" as const } }
                : {}),
              ...(settings.effort === null
                ? {}
                : { output_config: { effort: settings.effort } }),
              max_tokens:
                responseTokenLimit(settings, request.generation?.maxTokens) ??
                body.max_tokens,
            }),
          ),
        ),
    },
  });
  return Route.make({
    id: native.id,
    provider: native.provider,
    protocol: recoverMessages(configured),
    endpoint: native.endpoint,
    auth: native.auth,
    framing: Framing.sse,
    headers: () => ({ "anthropic-version": "2023-06-01" }),
    defaults: native.defaults,
  }).model({ id: model });
};
