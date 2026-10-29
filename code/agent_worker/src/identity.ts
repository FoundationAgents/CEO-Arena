import { InvalidRequestReason, LLMError, Model } from "@opencode-ai/llm";
import { ProviderShared } from "@opencode-ai/llm/protocols/shared";
import { Effect, Stream } from "effect";

export interface ModelIdentityObservation {
  readonly requested_model: string;
  readonly served_model: string | null;
  readonly expected_model_ids: ReadonlyArray<string>;
  readonly echoed_effort: string | null;
  readonly protocol: string;
  readonly status: "matched" | "mismatch" | "missing" | "unrestricted";
}

/** A routing-integrity failure is not a model decision or a retryable outage. */
export class ProviderIdentityError extends LLMError {
  constructor(readonly observation: ModelIdentityObservation) {
    super({
      module: "ProviderIdentity",
      method: "validate",
      reason: new InvalidRequestReason({
        parameter: "model",
        message:
          observation.status === "missing"
            ? "Provider completed a response without a serving model identity"
            : `Requested ${observation.requested_model}; provider reported ${observation.served_model}`,
      }),
    });
  }
}

const MODEL_ID = /^(?!sk-)[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$/;
const EFFORTS = new Set(["none", "minimal", "low", "medium", "high", "xhigh", "max"]);
const modelId = (value: unknown): string | null =>
  typeof value === "string"
    ? MODEL_ID.test(value) ? value : "<invalid-model-id>"
    : null;

const record = (value: unknown): Readonly<Record<string, unknown>> =>
  ProviderShared.isRecord(value) ? value : {};

/** Validate raw frames before provider schemas discard identity metadata. */
class ResponseIdentityGuard {
  private served: string | null = null;
  private effort: string | null = null;
  private lastObservation: string | undefined;
  private providerFailed = false;

  constructor(
    private readonly requested: string,
    private readonly expected: ReadonlyArray<string>,
    private readonly protocol: string,
    private readonly observer?: (event: ModelIdentityObservation) => void,
  ) {}

  inspect(frame: unknown): ProviderIdentityError | undefined {
    let value: unknown = frame;
    if (typeof frame === "string") {
      try { value = JSON.parse(frame); }
      catch { return undefined; }
    }
    const event = record(value);
    if (event.type === "error" || event.type === "response.failed" || event.error) {
      this.providerFailed = true;
      return undefined;
    }
    const response = record(event.response);
    const message = record(event.message);
    const identity = modelId(response.model ?? message.model ?? event.model);
    const rawEffort = record(response.reasoning).effort ??
      record(message.output_config).effort ?? record(event.output_config).effort ??
      response.reasoning_effort ?? event.reasoning_effort;
    if (typeof rawEffort === "string" && EFFORTS.has(rawEffort))
      this.effort = rawEffort;
    if (identity !== null) this.served = identity;

    if (this.expected.length && identity !== null && !this.expected.includes(identity))
      return this.reject("mismatch");
    if (this.served !== null) this.observe(this.expected.length ? "matched" : "unrestricted");

    const choices = Array.isArray(event.choices) ? event.choices : [];
    const complete = event.type === "response.completed" ||
      event.type === "response.incomplete" || event.type === "message_stop" ||
      (event.type === "message_delta" && !!record(event.delta).stop_reason) ||
      choices.some((choice) => !!record(choice).finish_reason);
    if (complete && !this.providerFailed && this.expected.length && this.served === null)
      return this.reject("missing");
    return undefined;
  }

  private reject(status: "mismatch" | "missing"): ProviderIdentityError {
    return new ProviderIdentityError(this.observe(status));
  }

  private observe(status: ModelIdentityObservation["status"]): ModelIdentityObservation {
    const observation: ModelIdentityObservation = {
      requested_model: modelId(this.requested)!,
      served_model: this.served,
      expected_model_ids: this.expected.map((id) => modelId(id)!),
      echoed_effort: this.effort,
      protocol: this.protocol,
      status,
    };
    const serialized = JSON.stringify(observation);
    if (serialized !== this.lastObservation) {
      this.observer?.(observation);
      this.lastObservation = serialized;
    }
    return observation;
  }
}

/** Preserve the native route while checking each response's reported identity. */
export const withIdentity = (
  model: Model,
  expectedModelIds: ReadonlyArray<string>,
  observer?: (event: ModelIdentityObservation) => void,
): Model => {
  const transport = model.route.transport;
  const route = model.route.with({
    transport: {
      ...transport,
      frames: (prepared, request, runtime) => Stream.suspend(() => {
        const guard = new ResponseIdentityGuard(
          model.id, expectedModelIds, model.route.protocol, observer,
        );
        return transport.frames(prepared, request, runtime).pipe(
          Stream.mapEffect((frame: unknown) => Effect.suspend(() => {
            const error = guard.inspect(frame);
            return error ? Effect.fail(error) : Effect.succeed(frame);
          })),
        );
      }),
    },
  });
  return Model.update(model, { route });
};
