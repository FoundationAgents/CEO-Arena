import { readFileSync } from "node:fs";

import { Schema } from "effect";

import { InferenceSettings, resolveInference, responseTokenLimit } from "../src/inference";

const Input = Schema.Array(Schema.Struct({
  model: Schema.String,
  settings: Schema.NullOr(InferenceSettings),
}));
const inputs = Schema.decodeUnknownSync(Input)(JSON.parse(readFileSync(0, "utf8")));
const resolved = inputs.map(({ model, settings }) => {
  const profile = resolveInference(model, settings ?? undefined);
  return {
    profile,
    limits: [null, 2_048, 131_072].map((limit) => responseTokenLimit(profile, limit) ?? null),
  };
});
process.stdout.write(JSON.stringify(resolved));
