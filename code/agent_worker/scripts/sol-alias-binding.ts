/** Offline binding calculation using the same codecs and serializer as the worker. */
import { readFileSync } from "node:fs";

import { Schema } from "effect";

import { ConversationJournal } from "../src/conversation";
import { InferenceSettings, resolveInference } from "../src/inference";
import { DECISION_PROTOCOL_VERSION, WorkerDecisionRequest } from "../src/protocol";

const Input = Schema.Struct({
  message: WorkerDecisionRequest,
  before: InferenceSettings,
  after: InferenceSettings,
});
const input = Schema.decodeUnknownSync(Schema.fromJsonString(Input))(readFileSync(0, "utf8"));
const issued = input.message.request;
const binding = (settings: InferenceSettings) => {
  const inference = resolveInference("gpt-5.6-sol", settings);
  return ConversationJournal.binding({
    protocol: DECISION_PROTOCOL_VERSION,
    model: "gpt-5.6-sol",
    route: inference.protocol,
    inference,
    run: issued.run_id,
    company: issued.company_id,
    observation: issued.observation,
    limits: issued.budget,
  });
};
process.stdout.write(JSON.stringify({ before: binding(input.before), after: binding(input.after) }));
