import { LLMClient, LLMEvent, LLMRequest } from "@opencode-ai/llm";
import { Effect, Layer, Stream } from "effect";

import { CompanyWorkspace } from "../src/memory";
import { runDecision } from "../src/runner";
import { decisionFixture } from "../src/testing";
import { TraceRecorder } from "../src/trace";

class CrashAfterFileWrite extends TraceRecorder {
  override record(kind: string, data: unknown): void {
    if (
      kind !== "tool-event" ||
      typeof data !== "object" ||
      !data ||
      !("event" in data)
    )
      return;
    const event = data.event;
    if (
      typeof event === "object" &&
      event &&
      "type" in event &&
      "name" in event &&
      event.type === "tool-result" &&
      event.name === "edit_file"
    )
      process.exit(86);
  }
}

const input = decisionFixture();
const crash = process.argv[3] === "crash";
const workspace = new CompanyWorkspace(
  process.argv[2]!,
  input.request.run_id,
  input.request.company_id,
);
const call = (name: string, arguments_: unknown = {}) =>
  LLMEvent.toolCall({ id: name, name, input: arguments_ });
let requests = 0;
const client = Layer.succeed(LLMClient.Service, {
  prepare: LLMClient.prepare,
  stream(_request: LLMRequest) {
    requests++;
    return Stream.fromIterable([
      ...(crash
        ? [
            call("write_file", { path: "counter.txt", content: "x" }),
            call("edit_file", {
              path: "counter.txt",
              old_string: "x",
              new_string: "xx",
            }),
            call("set_product", { product_id: "basic", price_cents: 1500 }),
          ]
        : [call("seal_decision")]),
      LLMEvent.finish({ reason: "tool-calls", usage: { outputTokens: 10 } }),
    ]);
  },
  generate: () => Effect.die("stream only"),
});
const result = await Effect.runPromise(
  runDecision(
    input,
    { apiKey: "offline", baseURL: "http://127.0.0.1:1/v1", model: "offline" },
    {
      query: async () => {
        throw new Error("unexpected query");
      },
      validate: async () => {},
    },
    crash ? new CrashAfterFileWrite("offline") : new TraceRecorder("offline"),
    undefined,
    { workspace },
  ).pipe(Effect.provide(client)),
);
process.stdout.write(
  JSON.stringify({ result, requests, file: workspace.read("counter.txt") }),
);
