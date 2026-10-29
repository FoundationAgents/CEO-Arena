import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

import {
  LLMClient,
  LLMError,
  LLMRequest,
  LLMResponse,
  Message,
  ToolDefinition,
  TransportReason,
  UnknownProviderReason,
  type LLMEvent,
  type Usage,
} from "@opencode-ai/llm";
import { RequestExecutor } from "@opencode-ai/llm/route";
import { Effect, Exit, Layer, Schema, Stream } from "effect";
import { FetchHttpClient, HttpClient } from "effect/unstable/http";

import { describeFailure, ProviderFault } from "../src/failure";
import type { ModelIdentityObservation } from "../src/identity";
import { InferenceSettings } from "../src/inference";
import { NewApiModel } from "../src/provider";

const Input = Schema.Struct({
  paid: Schema.Boolean,
  routes: Schema.Array(
    Schema.Struct({
      model: Schema.String,
      apiKey: Schema.String,
      baseURL: Schema.String,
      inference: InferenceSettings,
    }),
  ),
});
type RouteInput = (typeof Input.Type)["routes"][number];
type ProbeStatus =
  | "prepared"
  | "passed"
  | "provider_error"
  | "incomplete"
  | "unexpected_tools";
interface ProbeTurn {
  readonly index: number;
  readonly elapsed_seconds: number;
  readonly request_body_sha256: string;
  readonly request_fields: ReadonlyArray<string>;
  readonly finish_reason: string | null;
  readonly tool_names: ReadonlyArray<string>;
  readonly input_tokens: number | null;
  readonly output_tokens: number | null;
  readonly reasoning_tokens: number | null;
  readonly cache_read_tokens: number | null;
  readonly cache_write_tokens: number | null;
}
interface ProbeFailure {
  readonly category: string;
  readonly reason: string | null;
  readonly http_status: number | null;
}
interface ProbeResult {
  readonly model: string;
  readonly inference: InferenceSettings;
  readonly status: ProbeStatus;
  readonly valid_seal: boolean;
  readonly issued_requests: number;
  readonly completed_rounds: number;
  readonly elapsed_seconds: number;
  readonly identities: ReadonlyArray<ModelIdentityObservation>;
  readonly turns: ReadonlyArray<ProbeTurn>;
  readonly failure: ProbeFailure | null;
}

const emptyInput = {
  type: "object",
  properties: {},
  additionalProperties: false,
} as const;
const tools = [
  ToolDefinition.make({
    name: "query_company",
    description:
      "Return one synthetic test value; only SELECT 1 and SELECT 2 are valid.",
    inputSchema: {
      type: "object",
      properties: { sql: { type: "string" } },
      required: ["sql"],
      additionalProperties: false,
    },
  }),
  ToolDefinition.make({
    name: "preview_decision",
    description:
      "Preview the synthetic empty decision after both test queries returned.",
    inputSchema: emptyInput,
  }),
  ToolDefinition.make({
    name: "seal_decision",
    description: "Finalize the synthetic test only after preview has returned.",
    inputSchema: emptyInput,
  }),
];
const system =
  "This is a tiny API integration test, not an economic experiment. Follow exactly three assistant tool turns. First: issue query_company twice in ONE reply, sql SELECT 1 and SELECT 2. After both results: issue only preview_decision({}). After its result: issue only seal_decision({}). Do not combine stages. No explanation is needed. Keep any reasoning brief.";
// The SDK executor retries status failures internally; this paid probe must not.
const singleAttempt = Layer.effect(
  RequestExecutor.Service,
  Effect.gen(function* () {
    const http = yield* HttpClient.HttpClient;
    return RequestExecutor.Service.of({
      execute: (request) =>
        http.execute(request).pipe(
          Effect.mapError(
            () =>
              new LLMError({
                module: "NativeInferenceProbe",
                method: "request",
                reason: new TransportReason({
                  message: "Probe transport failed",
                  kind: "TransportError",
                }),
              }),
          ),
          Effect.flatMap((response) =>
            response.status < 400
              ? Effect.succeed(response)
              : Effect.fail(
                  new LLMError({
                    module: "NativeInferenceProbe",
                    method: "request",
                    reason: new UnknownProviderReason({
                      message: `Provider HTTP ${response.status}`,
                      status: response.status,
                    }),
                  }),
                ),
          ),
        ),
    });
  }),
).pipe(Layer.provide(FetchHttpClient.layer));
const layer = LLMClient.layer.pipe(Layer.provide(singleAttempt));
const emit = (event: object): void => {
  process.stdout.write(JSON.stringify(event) + "\n");
};
const elapsed = (start: number): number =>
  Math.round((performance.now() - start) / 10) / 100;
const usageFields = (usage?: Usage) => ({
  input_tokens: usage?.inputTokens ?? null,
  output_tokens: usage?.outputTokens ?? null,
  reasoning_tokens: usage?.reasoningTokens ?? null,
  cache_read_tokens: usage?.cacheReadInputTokens ?? null,
  cache_write_tokens: usage?.cacheWriteInputTokens ?? null,
});

/** Exercise native signatures and multi-tool continuations within a fixed tiny allowance. */
class NativeInferenceProbe {
  private readonly identities: ModelIdentityObservation[] = [];
  private readonly turns: ProbeTurn[] = [];
  private readonly started = performance.now();
  private readonly model: NewApiModel;
  private issued = 0;
  private completed = 0;

  constructor(
    private readonly route: RouteInput,
    private readonly paid: boolean,
  ) {
    this.model = new NewApiModel({
      ...route,
      onIdentity: (event) => this.identities.push(event),
    });
  }

  async run(): Promise<ProbeResult> {
    let request = this.model.request({
      system,
      prompt: "Start the first stage now.",
      tools,
      maxOutputTokens: 2048,
    });
    for (let round = 0; round < (this.paid ? 3 : 1); round++) {
      const start = performance.now();
      const prepared = await Effect.runPromise(
        LLMClient.prepare(request).pipe(Effect.provide(layer)),
      );
      const encoded = JSON.stringify(prepared.body);
      const preparedFields = {
        request_body_sha256: createHash("sha256").update(encoded).digest("hex"),
        request_fields: Object.keys(JSON.parse(encoded)).sort(),
      };
      if (!this.paid) {
        this.turns.push({
          index: 0,
          elapsed_seconds: elapsed(start),
          ...preparedFields,
          finish_reason: null,
          tool_names: [],
          ...usageFields(),
        });
        return this.result("prepared");
      }
      this.issued++;
      const events: LLMEvent[] = [];
      const result = await Effect.runPromise(
        Effect.exit(
          LLMClient.stream(request).pipe(
            Stream.tap((event) =>
              Effect.sync(() => {
                events.push(event);
              }),
            ),
            Stream.runCollect,
            Effect.provide(layer),
            Effect.timeoutOrElse({
              duration: "60 seconds",
              orElse: () =>
                Effect.fail(
                  new ProviderFault({
                    category: "transport",
                    message: "Probe request timed out after 60 seconds",
                    retryable: false,
                    reason: "ProbeTimeout",
                  }),
                ),
            }),
          ),
        ),
      );
      const response = LLMResponse.fromEvents(events);
      this.turns.push({
        index: round + 1,
        elapsed_seconds: elapsed(start),
        ...preparedFields,
        finish_reason: response?.finishReason ?? null,
        tool_names:
          response?.toolCalls.map((call) =>
            tools.some((tool) => tool.name === call.name)
              ? call.name
              : "<unknown-tool>",
          ) ?? [],
        ...usageFields(response?.usage),
      });
      emit({
        event: "request_finished",
        model: this.route.model,
        round: round + 1,
        elapsed_seconds: elapsed(start),
        output_tokens: response?.usage?.outputTokens ?? null,
      });
      if (Exit.isFailure(result)) {
        const failure = describeFailure(result.cause);
        return this.result("provider_error", {
          category: failure.category,
          reason: failure.reason ?? null,
          http_status: failure.http_status ?? null,
        });
      }
      if (
        !response ||
        response.finishReason !== "tool-calls" ||
        response.usage?.outputTokens === undefined
      )
        return this.result("incomplete");
      if (!this.validCalls(response, round))
        return this.result("unexpected_tools");
      this.completed++;
      if (round === 2) return this.result("passed");
      request = LLMRequest.update(request, {
        messages: [
          ...request.messages,
          response.message,
          ...response.toolCalls.map((call) =>
            Message.tool({
              id: call.id,
              name: call.name,
              result:
                round === 0
                  ? {
                      value:
                        (call.input as { sql: string }).sql === "SELECT 1"
                          ? 1
                          : 2,
                    }
                  : { valid: true, empty_decision: true },
            }),
          ),
          ...(round === 1
            ? [
                Message.user(
                  "Closing stage: do not query again. Call only seal_decision({}).",
                ),
              ]
            : []),
        ],
      });
    }
    return this.result("incomplete");
  }

  private validCalls(response: LLMResponse, round: number): boolean {
    const calls = response.toolCalls;
    if (round === 0)
      return (
        calls.length === 2 &&
        calls.every((call) => call.name === "query_company") &&
        ["SELECT 1", "SELECT 2"].every((sql) =>
          calls.some(
            (call) =>
              typeof call.input === "object" &&
              call.input !== null &&
              Object.keys(call.input).length === 1 &&
              "sql" in call.input &&
              call.input.sql === sql,
          ),
        )
      );
    return (
      calls.length === 1 &&
      calls[0]!.name === (round === 1 ? "preview_decision" : "seal_decision") &&
      typeof calls[0]!.input === "object" &&
      calls[0]!.input !== null &&
      Object.keys(calls[0]!.input).length === 0
    );
  }

  private result(
    status: ProbeStatus,
    failure: ProbeFailure | null = null,
  ): ProbeResult {
    return {
      model: this.route.model,
      inference: this.model.inference,
      status,
      valid_seal: status === "passed",
      issued_requests: this.issued,
      completed_rounds: this.completed,
      elapsed_seconds: elapsed(this.started),
      identities: this.identities,
      turns: this.turns,
      failure,
    };
  }
}

async function main(): Promise<void> {
  const input = Schema.decodeUnknownSync(Input)(
    JSON.parse(readFileSync(0, "utf8")),
  );
  const started = performance.now();
  const active = new Set<string>();
  const results: ProbeResult[] = [];
  let index = 0;
  const deadline = setTimeout(() => {
    process.stderr.write(
      "Native inference probe exceeded its 10-minute total limit.\n",
    );
    process.exit(1);
  }, 600_000);
  const timer = setInterval(
    () =>
      emit({
        event: "progress",
        active_models: [...active],
        elapsed_seconds: elapsed(started),
      }),
    20_000,
  );
  try {
    await Promise.all(
      Array.from({ length: Math.min(3, input.routes.length) }, async () => {
        while (index < input.routes.length) {
          const position = index++;
          const route = input.routes[position]!;
          active.add(route.model);
          const result = await new NativeInferenceProbe(
            route,
            input.paid,
          ).run();
          active.delete(route.model);
          results[position] = result;
          emit({
            event: "model_finished",
            model: route.model,
            status: result.status,
            completed_rounds: result.completed_rounds,
          });
        }
      }),
    );
    emit({ event: "report", results });
  } finally {
    clearInterval(timer);
    clearTimeout(deadline);
  }
}

main().catch(() => {
  process.stderr.write(
    "Native inference probe failed; no credentials or provider payloads were logged.\n",
  );
  process.exitCode = 1;
});
