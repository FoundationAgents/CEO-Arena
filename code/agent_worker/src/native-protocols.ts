import * as Chat from "@opencode-ai/llm/protocols/openai-chat";
import * as Responses from "@opencode-ai/llm/protocols/openai-responses";
import { ProviderShared } from "@opencode-ai/llm/protocols/shared";
import { Protocol } from "@opencode-ai/llm/route";
import { Effect, Schema } from "effect";

import {
  type InferenceSettings,
  ReasoningEffort,
  responseTokenLimit,
} from "./inference";
import {
  recoveringChatProtocol,
  recoveringResponsesProtocol,
} from "./recovery";

const strict = { onExcessProperty: "error" } as const;
const Reasoning = Schema.Struct({
  effort: Schema.optional(ReasoningEffort),
  summary: Schema.optional(Schema.Literal("auto")),
});
const acceptsReasoning = Schema.is(
  Reasoning.annotate({ parseOptions: strict }),
);
const acceptsResponses = Schema.is(
  Responses.protocol.body.schema.annotate({ parseOptions: strict }),
);
type ResponsesBody = Omit<Responses.OpenAIResponsesBody, "reasoning"> & {
  readonly reasoning?: typeof Reasoning.Type;
};
const ResponsesBody = Schema.declare<ResponsesBody>(
  (value): value is ResponsesBody =>
    ProviderShared.isRecord(value) &&
    (value.reasoning === undefined || acceptsReasoning(value.reasoning)) &&
    acceptsResponses({ ...value, reasoning: undefined }),
);
const ChatShape = Schema.Struct({
  ...Chat.bodyFields,
  reasoning_effort: Schema.optional(ReasoningEffort),
  thinking: Schema.optional(Schema.Struct({ type: Schema.Literal("enabled") })),
  enable_thinking: Schema.optional(Schema.Boolean),
  preserve_thinking: Schema.optional(Schema.Boolean),
  max_completion_tokens: Schema.optional(Schema.Number),
});
type ChatBody = typeof ChatShape.Type;
const acceptsChat = Schema.is(ChatShape.annotate({ parseOptions: strict }));
const ChatBody = Schema.declare<ChatBody>(
  (value): value is ChatBody =>
    acceptsChat(value) &&
    !(
      value.max_tokens !== undefined &&
      value.max_completion_tokens !== undefined
    ),
);

/** Extend typed wire bodies after SDK message lowering, without changing the vendor. */
export const responsesProtocol = (settings: InferenceSettings) =>
  Protocol.make({
    ...recoveringResponsesProtocol,
    body: {
      schema: ResponsesBody,
      from: (request) =>
        Responses.protocol.body.from(request).pipe(
          Effect.map((body): ResponsesBody => {
            const { reasoning: _reasoning, ...common } = body;
            return {
              ...common,
              store: false,
              include: ["reasoning.encrypted_content"],
              ...(settings.effort === null
                ? {}
                : { reasoning: { effort: settings.effort } }),
              max_output_tokens: responseTokenLimit(
                settings,
                request.generation?.maxTokens,
              ),
            };
          }),
        ),
    },
  });

export const chatProtocol = (settings: InferenceSettings) =>
  Protocol.make({
    ...recoveringChatProtocol,
    body: {
      schema: ChatBody,
      from: (request) =>
        Chat.protocol.body.from(request).pipe(
          Effect.map((body): ChatBody => {
            const {
              max_tokens: _maxTokens,
              reasoning_effort: _effort,
              ...common
            } = body;
            const limit = responseTokenLimit(
              settings,
              request.generation?.maxTokens,
            );
            return {
              ...common,
              ...(settings.effort === null
                ? {}
                : { reasoning_effort: settings.effort }),
              ...(settings.thinking === "enabled"
                ? { thinking: { type: "enabled" as const } }
                : {}),
              ...(settings.enable_thinking ? { enable_thinking: true } : {}),
              ...(settings.preserve_thinking
                ? { preserve_thinking: true }
                : {}),
              ...(settings.output_token_field === "max_completion_tokens"
                ? { max_completion_tokens: limit }
                : { max_tokens: limit }),
            };
          }),
        ),
    },
  });
