import { createInterface } from "node:readline";

import { LLMClient } from "@opencode-ai/llm";
import { RequestExecutor } from "@opencode-ai/llm/route";
import { Effect, Exit, Layer, ManagedRuntime, Schema } from "effect";

import { RpcCompanyBridge } from "./bridge";
import { installNewApiStreamCompatibility } from "./compatibility";
import { describeFailure } from "./failure";
import { InferenceSettings } from "./inference";
import { CompanyWorkspace } from "./memory";
import {
  DECISION_PROTOCOL_VERSION,
  decodeWorkerInput,
  encodeLine,
  type DecisionRequest,
  type DecisionResult,
  type ProviderFailure,
} from "./protocol";
import { OPENCODE_LLM_COMMIT } from "./provenance";
import { runDecision, type ProviderConfig } from "./runner";
import { TraceRecorder, tracePath } from "./trace";

installNewApiStreamCompatibility();

interface WorkerConfig extends ProviderConfig {
  readonly companyID: string;
  readonly parentPID: number;
  readonly traceDirectory?: string;
  readonly memoryDirectory: string;
}

interface ActiveDecision {
  readonly requestID: string;
  readonly queries: RpcCompanyBridge;
}

const requireEnvironment = (name: string) => {
  const value = process.env[name];
  if (!value) throw new Error(`missing required worker environment: ${name}`);
  return value;
};

const loadConfig = (): WorkerConfig => {
  if (requireEnvironment("CEO_ARENA_OPENCODE_COMMIT") !== OPENCODE_LLM_COMMIT)
    throw new Error("OpenCode provenance mismatch");
  const parentPID = Number(requireEnvironment("CEO_ARENA_PARENT_PID"));
  if (!Number.isSafeInteger(parentPID) || parentPID <= 0)
    throw new Error("invalid parent process identity");
  return {
    apiKey: requireEnvironment("CEO_ARENA_API_KEY"),
    baseURL: requireEnvironment("CEO_ARENA_BASE_URL"),
    companyID: requireEnvironment("CEO_ARENA_COMPANY_ID"),
    model: requireEnvironment("CEO_ARENA_MODEL"),
    inference: Schema.decodeUnknownSync(Schema.fromJsonString(InferenceSettings))(
      requireEnvironment("CEO_ARENA_INFERENCE"),
    ),
    parentPID,
    traceDirectory: process.env.CEO_ARENA_TRACE_DIR,
    memoryDirectory: requireEnvironment("CEO_ARENA_MEMORY_DIR"),
  };
};

const send = (message: unknown) => process.stdout.write(encodeLine(message));

const executorLayer = RequestExecutor.fetchLayer;
const llmLayer = LLMClient.layer.pipe(Layer.provide(executorLayer));
const runtimeLayer = Layer.mergeAll(executorLayer, llmLayer);
const runtime = ManagedRuntime.make(runtimeLayer);

const providerFailure = (
  started: number,
  failure: ProviderFailure,
): DecisionResult => ({
  status: "provider_error",
  diagnostics: { protocol_errors: 0, tool_errors: 0, recovered: false },
  usage: {
    provider_turns: 0,
    finalization_turns: 0,
    tool_calls: 0,
    input_tokens: 0,
    output_tokens: 0,
    reasoning_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    latency_ms: Math.max(0, Date.now() - started),
  },
  error: failure.message,
  failure,
});

const handleDecision = async (
  message: DecisionRequest,
  config: WorkerConfig,
  queries: RpcCompanyBridge,
) => {
  const started = Date.now();
  const recorder = new TraceRecorder(
    config.apiKey,
    tracePath(
      config.traceDirectory,
      message.request.run_id,
      message.request.company_id,
      message.request.world_version,
      message.request_id,
    ),
  );
  try {
    const exit = await runtime.runPromiseExit(
      runDecision(message, config, queries, recorder, undefined, {
        workspace: new CompanyWorkspace(
          config.memoryDirectory,
          message.request.run_id,
          message.request.company_id,
        ),
      }),
    );
    const result = Exit.isSuccess(exit)
      ? exit.value
      : providerFailure(started, describeFailure(exit.cause));
    if (Exit.isFailure(exit))
      recorder.record("provider-failure", result.failure);
    const receipt = await recorder.flush();
    send({
      type: "result",
      request_id: message.request_id,
      result: {
        ...result,
        trace_path: receipt?.path,
        trace_sha256: receipt?.sha256,
      },
    });
  } finally {
    recorder.close();
  }
};

const main = async () => {
  const config = loadConfig();
  const parentWatch = setInterval(() => {
    try {
      process.kill(config.parentPID, 0);
    } catch {
      process.stderr.write("parent process exited\n");
      process.exit(70);
    }
  }, 1_000);
  parentWatch.unref();
  send({
    type: "ready",
    company_id: config.companyID,
    opencode_commit: OPENCODE_LLM_COMMIT,
    decision_protocol_version: DECISION_PROTOCOL_VERSION,
  });
  const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
  let active: ActiveDecision | undefined;
  let lastFinishedRequestID: string | undefined;
  let shutdownRequested = false;
  const running = new Set<Promise<void>>();

  for await (const line of input) {
    const decoded = Effect.runSync(
      Effect.try({
        try: () => decodeWorkerInput(line),
        catch: () => new Error("invalid worker input"),
      }).pipe(Effect.option),
    );
    if (decoded._tag === "None") {
      process.stderr.write("invalid NDJSON protocol input\n");
      process.exitCode = 2;
      break;
    }
    const message = decoded.value;
    if (message.type === "shutdown") {
      shutdownRequested = true;
      if (active)
        process.stderr.write("shutdown requested while a decision is active\n");
      break;
    }
    if (
      message.type === "query_result" ||
      message.type === "validation_result"
    ) {
      const current = active;
      if (current?.requestID !== message.request_id) {
        if (message.request_id === lastFinishedRequestID) continue;
        throw new Error("uncorrelated company RPC response");
      }
      if (message.type === "validation_result")
        current.queries.resolveValidation(message.validation_id, message.error);
      else
        current.queries.resolveQuery(
          message.query_id,
          message.result ?? undefined,
          message.error,
        );
      continue;
    }
    if (active) {
      process.stderr.write("concurrent decision requests are not supported\n");
      process.exitCode = 2;
      break;
    }
    if (message.request.company_id !== config.companyID) {
      send({
        type: "result",
        request_id: message.request_id,
        result: providerFailure(Date.now(), {
          category: "internal",
          message: "Worker company identity mismatch",
          retryable: false,
        }),
      });
      continue;
    }

    const queries = new RpcCompanyBridge(message.request_id, send);
    active = { requestID: message.request_id, queries };
    const task = handleDecision(message, config, queries).finally(() => {
      queries.close();
      lastFinishedRequestID = message.request_id;
      active = undefined;
      running.delete(task);
    });
    running.add(task);
  }

  if (active && !shutdownRequested) process.exit(70);
  await Promise.allSettled(running);
  clearInterval(parentWatch);
};

void main()
  .finally(() => runtime.dispose())
  .catch(() => {
    process.stderr.write("agent worker failed during initialization\n");
    process.exitCode = 1;
  });
