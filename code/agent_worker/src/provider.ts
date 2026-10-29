import { LLM, type Model, type ToolDefinition } from "@opencode-ai/llm";
import * as OpenAI from "@opencode-ai/llm/providers/openai";
import * as OpenAICompatible from "@opencode-ai/llm/providers/openai-compatible";
import { Framing, Route } from "@opencode-ai/llm/route";

import { fableModel } from "./fable";
import { withIdentity, type ModelIdentityObservation } from "./identity";
import {
  type InferenceSettings,
  resolveInference,
  responseTokenLimit,
} from "./inference";
import { chatProtocol, responsesProtocol } from "./native-protocols";

export interface ProviderConfig {
  readonly apiKey: string;
  readonly baseURL: string;
  readonly model: string;
  readonly inference?: InferenceSettings;
  readonly onIdentity?: (event: ModelIdentityObservation) => void;
}

/** Select a native wire protocol while keeping the shared CEO tool loop intact. */
export class NewApiModel {
  readonly inference: InferenceSettings;
  readonly protocol: InferenceSettings["protocol"];
  private readonly model: Model;

  get id(): string {
    return this.model.id;
  }

  get singleResponseLimit(): number | undefined {
    return this.inference.max_output_tokens ?? undefined;
  }

  constructor(config: ProviderConfig) {
    const auth = { apiKey: config.apiKey, baseURL: config.baseURL };
    this.inference = resolveInference(config.model, config.inference);
    this.protocol = this.inference.protocol;
    let model: Model;
    if (this.protocol === "anthropic-messages") {
      model = fableModel(auth, config.model, this.inference);
    } else {
      const native =
        this.protocol === "openai-responses"
          ? OpenAI.configure(auth).responses(config.model).route
          : OpenAICompatible.configure({ ...auth, provider: "newapi" }).model(
              config.model,
            ).route;
      const common = {
        id: native.id,
        provider: native.provider,
        endpoint: native.endpoint,
        auth: native.auth,
        framing: Framing.sse,
        defaults: native.defaults,
      };
      const route =
        this.protocol === "openai-responses"
          ? Route.make({
              ...common,
              protocol: responsesProtocol(this.inference),
            })
          : Route.make({ ...common, protocol: chatProtocol(this.inference) });
      model = route.model({ id: config.model });
    }
    this.model = withIdentity(
      model,
      this.inference.expected_model_ids,
      config.onIdentity,
    );
  }

  request(input: {
    readonly system: string;
    readonly prompt: string;
    readonly tools: ReadonlyArray<ToolDefinition>;
    readonly maxOutputTokens?: number | null;
  }) {
    const limit = responseTokenLimit(this.inference, input.maxOutputTokens);
    return LLM.request({
      model: this.model,
      system: input.system,
      prompt: input.prompt,
      tools: input.tools,
      toolChoice: "auto",
      // Keep native reasoning-bound prefixes append-only on continuation.
      cache: "none",
      generation: limit === undefined ? {} : { maxTokens: limit },
    });
  }
}
