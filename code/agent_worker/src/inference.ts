import { Schema } from "effect";

export const ReasoningEffort = Schema.Literals([
  "none",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
]);

export const InferenceSettings = Schema.Struct({
  protocol: Schema.Literals([
    "openai-responses",
    "anthropic-messages",
    "openai-compatible-chat",
  ]),
  effort: Schema.NullOr(ReasoningEffort),
  thinking: Schema.NullOr(Schema.Literals(["adaptive", "enabled"])),
  enable_thinking: Schema.Boolean,
  preserve_thinking: Schema.Boolean,
  output_token_field: Schema.Literals([
    "max_output_tokens",
    "max_tokens",
    "max_completion_tokens",
  ]),
  max_output_tokens: Schema.NullOr(Schema.Number),
  expected_model_ids: Schema.Array(Schema.String),
});
export type InferenceSettings = typeof InferenceSettings.Type;

const basic: InferenceSettings = {
  protocol: "openai-compatible-chat",
  effort: null,
  thinking: null,
  enable_thinking: false,
  preserve_thinking: false,
  output_token_field: "max_tokens",
  max_output_tokens: null,
  expected_model_ids: [],
};
const responses = {
  ...basic,
  protocol: "openai-responses",
  effort: "max",
  output_token_field: "max_output_tokens",
  max_output_tokens: 128_000,
} as const;
const messages = {
  ...basic,
  protocol: "anthropic-messages",
  effort: "max",
  thinking: "adaptive",
  max_output_tokens: 128_000,
} as const;
const deepseek = {
  ...basic,
  effort: "max",
  thinking: "enabled",
  max_output_tokens: 393_216,
} as const;
const profiles: ReadonlyMap<string, InferenceSettings> = new Map<
  string,
  InferenceSettings
>([
  ["gpt-6-astra", responses],
  ["gpt-5.6-sol", responses],
  ["claude-fable-5-1", messages],
  ["claude-sonnet-5", messages],
  ["deepseek-v4-pro", deepseek],
  ["deepseek-v4-flash", deepseek],
  [
    "qwen3.8-max",
    {
      ...basic,
      effort: "xhigh",
      enable_thinking: true,
      preserve_thinking: true,
      output_token_field: "max_completion_tokens",
      max_output_tokens: 65_536,
    },
  ],
  ["gemini-3.8-flash", { ...basic, effort: "high", max_output_tokens: 65_536 }],
  [
    "doubao-seed-1.8",
    {
      ...basic,
      effort: "high",
      thinking: "enabled",
      output_token_field: "max_completion_tokens",
      max_output_tokens: 65_536,
      expected_model_ids: ["doubao-seed-1.8", "doubao-seed-1-8-251228"],
    },
  ],
]);

/** Resolve only documented model profiles; never guess an unknown model's effort. */
export const resolveInference = (
  model: string,
  supplied?: InferenceSettings,
): InferenceSettings => {
  const profile = profiles.get(model);
  const settings = Schema.decodeUnknownSync(InferenceSettings, {
    onExcessProperty: "error",
  })(
    supplied ??
      (profile
        ? {
            ...profile,
            expected_model_ids: profile.expected_model_ids.length
              ? profile.expected_model_ids
              : [model],
          }
        : basic),
  );
  const {
    protocol,
    output_token_field: field,
    thinking,
    max_output_tokens: cap,
  } = settings;
  if (
    (cap !== null && (!Number.isSafeInteger(cap) || cap <= 0)) ||
    settings.expected_model_ids.some(
      (id) =>
        !id ||
        [...id].some((char) => /\s/.test(char) || char.charCodeAt(0) < 32),
    ) ||
    new Set(settings.expected_model_ids).size !==
      settings.expected_model_ids.length ||
    (protocol === "openai-responses" && field !== "max_output_tokens") ||
    (protocol === "anthropic-messages" && field !== "max_tokens") ||
    (protocol === "anthropic-messages" && settings.effort === "none") ||
    (protocol === "openai-compatible-chat" && field === "max_output_tokens") ||
    (thinking === "adaptive" && protocol !== "anthropic-messages") ||
    (thinking === "enabled" && protocol !== "openai-compatible-chat") ||
    (thinking !== null && settings.enable_thinking) ||
    ((settings.enable_thinking || settings.preserve_thinking) &&
      protocol !== "openai-compatible-chat")
  )
    throw new Error(
      "Invalid inference settings for the selected wire protocol",
    );
  const expected =
    settings.expected_model_ids.length || profile === undefined
      ? settings.expected_model_ids
      : profile.expected_model_ids.length
        ? profile.expected_model_ids
        : [model];
  return Object.freeze({
    ...settings,
    expected_model_ids: Object.freeze([...expected]),
  });
};

export const responseTokenLimit = (
  settings: InferenceSettings,
  requested?: number | null,
): number | undefined => {
  const limit = settings.max_output_tokens;
  if (requested == null) return limit ?? undefined;
  if (!Number.isSafeInteger(requested) || requested <= 0)
    throw new Error("Response output allowance must be a positive integer");
  return limit === null ? requested : Math.min(requested, limit);
};
