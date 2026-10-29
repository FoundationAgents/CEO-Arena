import {
  closeSync,
  fstatSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readSync,
  writeFileSync,
} from "node:fs";
import { createHash } from "node:crypto";
import path from "node:path";

import type { ProviderFailure } from "./protocol";

type JsonPrimitive = boolean | null | number | string;
export type JsonValue =
  | JsonPrimitive
  | JsonValue[]
  | { readonly [key: string]: JsonValue };

const SENSITIVE_KEY =
  /(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|secret|credential|signature)/i;
const MAX_EVENT_BYTES = 64 * 1024;
const MAX_TRACE_BYTES = 16 * 1024 * 1024;

interface TimingFields {
  readonly partial?: boolean;
  readonly usage_incomplete?: boolean;
  readonly purpose?: "decision";
  readonly turn?: number;
  readonly attempt?: number;
  readonly duration_ms?: number;
  readonly event_count?: number;
  readonly tool?: string;
  readonly status?: string;
  readonly output_tokens?: number;
  readonly input_tokens?: number;
  readonly tool_calls?: number;
  readonly failure?: ProviderFailure;
}

/** Own one append-only descriptor and its digest, including any existing prefix. */
class TraceFile {
  private descriptor: number | undefined;
  private readonly hash = createHash("sha256");
  bytes: number;

  constructor(readonly path: string) {
    this.descriptor = openSync(path, "a+");
    try {
      this.bytes = fstatSync(this.descriptor).size;
      const buffer = Buffer.allocUnsafe(64 * 1024);
      for (let offset = 0; offset < this.bytes; ) {
        const read = readSync(
          this.descriptor,
          buffer,
          0,
          Math.min(buffer.length, this.bytes - offset),
          offset,
        );
        if (!read) throw new Error("Trace prefix changed while opening");
        this.hash.update(buffer.subarray(0, read));
        offset += read;
      }
    } catch (error) {
      this.close();
      throw error;
    }
  }

  append(line: string) {
    if (this.descriptor === undefined) throw new Error("Trace is closed");
    const bytes = Buffer.from(line, "utf8");
    writeFileSync(this.descriptor, bytes);
    this.hash.update(bytes);
    this.bytes += bytes.length;
  }

  flush() {
    if (this.descriptor === undefined) throw new Error("Trace is closed");
    fsyncSync(this.descriptor);
    return { path: this.path, sha256: this.hash.copy().digest("hex") };
  }

  close() {
    if (this.descriptor === undefined) return;
    closeSync(this.descriptor);
    this.descriptor = undefined;
  }
}

export class TraceRecorder {
  private readonly file?: TraceFile;
  private timings?: TraceFile;
  private timingErrors = 0;
  private exhausted = false;

  constructor(
    private readonly secret: string,
    tracePath?: string,
  ) {
    if (tracePath) {
      mkdirSync(path.dirname(tracePath), { recursive: true });
      this.file = new TraceFile(tracePath);
      this.bestEffortTiming(() => {
        this.timings = new TraceFile(`${tracePath}.timing.jsonl`);
      });
    }
  }

  record(kind: string, payload: unknown) {
    if (!this.file) return;
    if (this.exhausted) return;
    const redacted = redact(payload, this.secret);
    const serialized = JSON.stringify({ kind, payload: redacted });
    const eventBytes = Buffer.byteLength(serialized, "utf8");
    const entry =
      eventBytes <= MAX_EVENT_BYTES
        ? serialized
        : JSON.stringify({
            kind,
            payload: {
              truncated: true,
              original_bytes: eventBytes,
              sha256: createHash("sha256").update(serialized).digest("hex"),
            },
          });
    const line = `${entry}\n`;
    const lineBytes = Buffer.byteLength(line, "utf8");
    if (this.file.bytes + lineBytes > MAX_TRACE_BYTES) {
      const marker = `${JSON.stringify({
        kind: "trace-limit",
        payload: { max_bytes: MAX_TRACE_BYTES },
      })}\n`;
      if (
        this.file.bytes + Buffer.byteLength(marker, "utf8") <=
        MAX_TRACE_BYTES
      )
        this.file.append(marker);
      this.exhausted = true;
      return;
    }
    this.file.append(line);
  }

  timing(phase: string, fields: TimingFields = {}) {
    this.bestEffortTiming(() =>
      this.timings?.append(
        `${JSON.stringify({ phase, monotonic_ms: performance.now(), unix_ms: Date.now(), ...fields })}\n`,
      ),
    );
  }

  get timingErrorCount() {
    return this.timingErrors;
  }

  private bestEffortTiming(operation: () => unknown) {
    try {
      operation();
    } catch {
      this.timingErrors++;
      const timings = this.timings;
      this.timings = undefined;
      try {
        timings?.close();
      } catch {
        /* Optional telemetry cannot fail the decision. */
      }
    }
  }

  async flush() {
    try {
      const receipt = this.file?.flush();
      this.bestEffortTiming(() => this.timings?.flush());
      return receipt;
    } finally {
      this.close();
    }
  }

  close() {
    try {
      this.file?.close();
    } finally {
      this.bestEffortTiming(() => this.timings?.close());
    }
  }
}

export const tracePath = (
  directory: string | undefined,
  runID: string,
  companyID: string,
  worldVersion: number,
  requestID: string,
) => {
  if (!directory) return undefined;
  const safe = (value: string) => value.replace(/[^a-zA-Z0-9._-]/g, "_");
  const requestHash = createHash("sha256")
    .update(requestID)
    .digest("hex")
    .slice(0, 16);
  return path.resolve(
    directory,
    safe(runID),
    `${safe(companyID)}-${worldVersion}-${requestHash}.ndjson`,
  );
};

export const redact = (
  value: unknown,
  secret: string,
  seen = new WeakSet<object>(),
): JsonValue => {
  if (value === null || typeof value === "boolean" || typeof value === "number")
    return value;
  if (typeof value === "string")
    return secret ? value.replaceAll(secret, "[REDACTED]") : value;
  if (typeof value === "bigint") return value.toString();
  if (
    typeof value === "undefined" ||
    typeof value === "function" ||
    typeof value === "symbol"
  )
    return String(value);
  if (value instanceof Error)
    return { name: value.name, message: redact(value.message, secret, seen) };
  if (seen.has(value)) return "[Circular]";
  seen.add(value);
  if (Array.isArray(value))
    return value.map((item) => redact(item, secret, seen));
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).map(([key, item]) => [
      key,
      SENSITIVE_KEY.test(key) ? "[REDACTED]" : redact(item, secret, seen),
    ]),
  );
};
