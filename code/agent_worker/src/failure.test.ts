import { expect, test } from "bun:test";
import { createHash } from "node:crypto";

import {
  InvalidProviderOutputReason,
  LLMError,
  ProviderInternalReason,
  TransportReason,
} from "@opencode-ai/llm";
import { Cause, Effect, Fiber } from "effect";

import { describeFailure, TransportRecovery } from "./failure";

test("invalid provider frames retain only an exact digest and safe error classification", () => {
  const raw = JSON.stringify({
    api_key: "private-key",
    prompt: "private-business-text",
  });
  const failure = describeFailure(
    Cause.fail(
      new LLMError({
        module: "ProviderShared",
        method: "stream",
        reason: new InvalidProviderOutputReason({
          message: "Invalid Chat stream event",
          raw,
        }),
      }),
    ),
  );
  expect(failure).toMatchObject({
    category: "stream_decode",
    retryable: false,
    frame_bytes: Buffer.byteLength(raw),
    frame_sha256: createHash("sha256").update(raw).digest("hex"),
  });
  expect(JSON.stringify(failure)).not.toContain("private-key");
  expect(JSON.stringify(failure)).not.toContain("private-business-text");
});

test("transport codes are retained without dumping request bodies or secret messages", () => {
  const failure = describeFailure(
    Cause.fail(
      new LLMError({
        module: "RequestExecutor",
        method: "execute",
        reason: new TransportReason({
          message: "private-key",
          kind: "ECONNRESET",
        }),
      }),
    ),
  );
  expect(failure).toMatchObject({
    category: "transport",
    retryable: true,
    reason: "Transport:ECONNRESET",
  });
  expect(JSON.stringify(failure)).not.toContain("private-key");
});

test("transport backoff is bounded in delay, not attempts, and cancellation is immediate", async () => {
  const recovery = new TransportRecovery();
  const failure = {
    category: "transport" as const,
    message: "unavailable",
    retryable: true,
  };
  expect(recovery.delay(failure, 1)).toBe(1000);
  expect(recovery.delay(failure, 10000)).toBe(60000);
  expect(recovery.delay({ ...failure, retry_after_seconds: 4 }, 1)).toBe(4000);
  expect(recovery.delay({ ...failure, retry_after_seconds: 120 }, 100)).toBe(
    120000,
  );
  const fiber = Effect.runFork(recovery.sleep(60000));
  const started = performance.now();
  await Effect.runPromise(Fiber.interrupt(fiber));
  expect(performance.now() - started).toBeLessThan(1000);
});

test.each([400, 401, 402, 403, 404, 422])("HTTP %i never retries even if a provider labels it internal", (status) => {
  const failure = describeFailure(Cause.fail(new LLMError({
    module: "Provider", method: "stream",
    reason: new ProviderInternalReason({ message: "secret payload", status }),
  })));
  expect(failure).toMatchObject({ http_status: status, retryable: false });
  expect(failure.message).not.toContain("secret payload");
});

test.each([408, 429, 500, 502, 503, 504])("HTTP %i remains eligible for bounded temporary recovery", (status) => {
  const failure = describeFailure(Cause.fail(new LLMError({
    module: "Provider", method: "stream",
    reason: new ProviderInternalReason({ message: "unavailable", status }),
  })));
  expect(failure).toMatchObject({ http_status: status, retryable: true });
});
