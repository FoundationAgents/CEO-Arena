import type { Model, ToolCallPart } from "@opencode-ai/llm";
import * as Chat from "@opencode-ai/llm/protocols/openai-chat";
import * as Responses from "@opencode-ai/llm/protocols/openai-responses";
import { ProviderShared } from "@opencode-ai/llm/protocols/shared";
import type { ToolStream } from "@opencode-ai/llm/protocols/utils/tool-stream";
import { Framing, Protocol, Route } from "@opencode-ai/llm/route";
import { Effect } from "effect";

/** Mark invalid JSON as opaque rejected data so the stream can reach usage. */
const preserveInvalid = (
  tool: ToolStream.PendingTool,
  raw = tool.input,
): ToolStream.PendingTool => {
  try {
    JSON.parse(raw || "{}");
    return tool;
  } catch {
    // This JSON string is a transport-only carrier, NEVER an executable
    // substitute for arguments. DecisionSession rejects the entire turn.
    return {
      ...tool,
      input: JSON.stringify(raw),
      providerMetadata: {
        ...tool.providerMetadata,
        ceo_arena: { invalidToolInput: raw },
      },
    };
  }
};

const preserveTools = <Key extends string | number>(
  tools: ToolStream.State<Key>,
  key?: Key,
) =>
  Object.fromEntries(
    Object.entries<ToolStream.PendingTool | undefined>(tools).map(
      ([id, tool]) => [
        id,
        tool && (key === undefined || id === String(key))
          ? preserveInvalid(tool)
          : tool,
      ],
    ),
  ) as ToolStream.State<Key>;

export const invalidToolInput = (call: ToolCallPart): string | undefined => {
  const raw = call.providerMetadata?.ceo_arena?.invalidToolInput;
  return typeof raw === "string" ? raw : undefined;
};

export const recoveringChatProtocol = Protocol.make({
  ...Chat.protocol,
  stream: {
    ...Chat.protocol.stream,
    step: (state, event) =>
      Effect.gen(function* () {
        if (!event.choices?.[0]?.finish_reason)
          return yield* Chat.protocol.stream.step(state, event);
        // The last frame may contain BOTH an argument delta and finish_reason.
        // Accumulate it first, then finalize, without changing the wire payload.
        const [accumulated, deltas] = yield* Chat.protocol.stream.step(state, {
          ...event,
          choices: event.choices.map((choice) => ({
            ...choice,
            finish_reason: null,
          })),
        });
        const [next, finished] = yield* Chat.protocol.stream.step(
          {
            ...accumulated,
            tools: preserveTools(accumulated.tools),
          },
          {
            ...event,
            choices: event.choices.map((choice) => ({ ...choice, delta: {} })),
          },
        );
        return [next, [...deltas, ...finished]] as const;
      }),
  },
});

export const recoveringResponsesProtocol = Protocol.make({
  ...Responses.protocol,
  stream: {
    ...Responses.protocol.stream,
    step: (state, event) => {
      const item = event.item;
      if (
        event.type !== "response.output_item.done" ||
        item?.type !== "function_call" ||
        !item.id
      )
        return Responses.protocol.stream.step(state, event);
      const pending = state.tools[item.id];
      if (!pending) return Responses.protocol.stream.step(state, event);
      const preserved = preserveInvalid(
        pending,
        item.arguments ?? pending.input,
      );
      return Responses.protocol.stream.step(
        {
          ...state,
          tools: { ...state.tools, [item.id]: preserved },
        },
        preserved.providerMetadata?.ceo_arena?.invalidToolInput === undefined
          ? event
          : { ...event, item: { ...item, arguments: preserved.input } },
      );
    },
  },
});

/** Decorate the project's native signed-Messages protocol, not the SDK checkout. */
export const recoverMessages = <
  Body,
  State extends { readonly tools: ToolStream.State<number> },
>(
  upstream: Protocol.Protocol<Body, string, string, State>,
) =>
  Protocol.make({
    ...upstream,
    stream: {
      ...upstream.stream,
      step: (state: State, frame: string) => {
        let event: unknown;
        try {
          event = JSON.parse(frame);
        } catch {
          return upstream.stream.step(state, frame);
        }
        if (
          ProviderShared.isRecord(event) &&
          event.type === "message_delta" &&
          ProviderShared.isRecord(event.delta) &&
          event.delta.stop_reason &&
          (!ProviderShared.isRecord(event.usage) ||
            !Number.isSafeInteger(event.usage.output_tokens))
        )
          return Effect.fail(
            ProviderShared.eventError(
              upstream.id,
              "Messages terminal response omitted output-token usage",
            ),
          );
        if (
          ProviderShared.isRecord(event) &&
          event.type === "content_block_stop" &&
          typeof event.index === "number"
        )
          state = { ...state, tools: preserveTools(state.tools, event.index) };
        return upstream.stream.step(state, frame);
      },
    },
  });

export const recoverModel = (model: Model, protocol: "chat" | "responses") => {
  const native = model.route;
  const common = {
    id: native.id,
    provider: native.provider,
    endpoint: native.endpoint,
    auth: native.auth,
    framing: Framing.sse,
    defaults: native.defaults,
  };
  const route =
    protocol === "chat"
      ? Route.make({ ...common, protocol: recoveringChatProtocol })
      : Route.make({ ...common, protocol: recoveringResponsesProtocol });
  return route.model({ id: model.id });
};
