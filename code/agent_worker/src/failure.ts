import { createHash } from "node:crypto";

import { LLMError } from "@opencode-ai/llm";
import { Cause, Effect } from "effect";

import { ProviderIdentityError } from "./identity";
import type { ProviderFailure } from "./protocol";
import { PersistenceError } from "./storage";

const TRANSPORT_KINDS = new Set([
  "Timeout",
  "TransportError",
  "EncodeError",
  "DecodeError",
  "InvalidUrlError",
  "ECONNRESET",
  "ECONNREFUSED",
  "ETIMEDOUT",
  "ENOTFOUND",
  "EAI_AGAIN",
]);

/** A classified provider failure contains no request body, headers or response text. */
export class ProviderFault extends Error {
  constructor(readonly failure: ProviderFailure) {
    super(failure.message);
  }
}

export const describeFailure = (
  cause: Cause.Cause<unknown>,
): ProviderFailure => {
  const error =
    cause.reasons.find(Cause.isFailReason)?.error ??
    cause.reasons.find(Cause.isDieReason)?.defect;
  if (error instanceof ProviderFault) return error.failure;
  if (error instanceof ProviderIdentityError)
    return {
      category: "identity",
      message: error.reason.message,
      retryable: false,
      reason: error.observation.status === "missing"
        ? "ServingModelMissing" : "ServingModelMismatch",
    };
  if (error instanceof PersistenceError)
    return {
      category: "worker",
      message: error.message,
      retryable: false,
      reason: "AgentPersistence",
    };
  if (!(error instanceof LLMError))
    return {
      category: "worker",
      message: "Worker execution failed",
      retryable: false,
      reason:
        error instanceof TypeError
          ? "TypeError"
          : error instanceof RangeError
            ? "RangeError"
            : "UnknownDefect",
    };
  const reason = error.reason;
  const status = "http" in reason ? reason.http?.response?.status : undefined;
  const httpStatus = status ?? ("status" in reason ? reason.status : undefined);
  const raw = "raw" in reason ? reason.raw : undefined;
  const kind =
    "kind" in reason && reason.kind && TRANSPORT_KINDS.has(reason.kind)
      ? reason.kind
      : undefined;
  const category: ProviderFailure["category"] =
    reason._tag === "Authentication"
      ? "auth"
      : reason._tag === "RateLimit"
        ? "rate_limit"
        : reason._tag === "Transport"
          ? "transport"
          : reason._tag === "InvalidProviderOutput"
            ? /Failed to read/.test(reason.message)
              ? "stream_read"
              : "stream_decode"
            : httpStatus !== undefined
              ? "http"
              : "unknown";
  return {
    category,
    message:
      httpStatus === undefined
        ? `Provider ${category} failure`
        : `Provider HTTP ${httpStatus}`,
    retryable:
      httpStatus !== undefined
        ? httpStatus === 408 || httpStatus === 429 || httpStatus >= 500
        : category === "transport" ||
          category === "stream_read" ||
          reason.retryable,
    http_status: httpStatus,
    reason: `${reason._tag}${kind ? `:${kind}` : ""}`,
    retry_after_seconds:
      error.retryAfterMs === undefined ? undefined : error.retryAfterMs / 1000,
    ...(raw === undefined
      ? {}
      : {
          frame_bytes: Buffer.byteLength(raw, "utf8"),
          frame_sha256: createHash("sha256").update(raw).digest("hex"),
        }),
  };
};

/** The worker alone retries these failures inside its existing conversation. */
export class TransportRecovery {
  constructor(
    private readonly baseDelayMs = 1000,
    private readonly maxDelayMs = 60_000,
  ) {}

  delay(failure: ProviderFailure, attempt: number): number {
    return Math.max(
      0,
      Math.min(
        this.maxDelayMs,
        this.baseDelayMs * 2 ** Math.min(attempt - 1, 6),
      ),
      (failure.retry_after_seconds ?? 0) * 1000,
    );
  }

  sleep(milliseconds: number) {
    return Effect.sleep(milliseconds);
  }
}
