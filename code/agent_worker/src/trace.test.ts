import { expect, spyOn, test } from "bun:test";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { redact, TraceRecorder } from "./trace";

test("redacts credentials while preserving usage token counters", () => {
  expect(
    redact(
      {
        api_key: "secret-value",
        access_token: "secret-value",
        usage: { inputTokens: 12, outputTokens: 7, reasoningTokens: 3 },
      },
      "secret-value",
    ),
  ).toEqual({
    api_key: "[REDACTED]",
    access_token: "[REDACTED]",
    usage: { inputTokens: 12, outputTokens: 7, reasoningTokens: 3 },
  });
});

test("flush durably returns the exact trace hash", async () => {
  const directory = mkdtempSync(path.join(tmpdir(), "ceo_arena-trace-"));
  const tracePath = path.join(directory, "trace.ndjson");
  try {
    const recorder = new TraceRecorder("secret-value", tracePath);
    recorder.record("event", { value: "safe" });

    const receipt = await recorder.flush();

    expect(receipt?.path).toBe(tracePath);
    expect(receipt?.sha256).toBe(
      createHash("sha256").update(readFileSync(tracePath)).digest("hex"),
    );
  } finally {
    rmSync(directory, { force: true, recursive: true });
  }
});

test("streaming trace hashing preserves exact bytes and resumes a large existing prefix", async () => {
  const directory = mkdtempSync(path.join(tmpdir(), "ceo_arena-trace-prefix-"));
  const file = path.join(directory, "trace.ndjson");
  const prefix = JSON.stringify({ kind: "old", payload: "历史记录" }) + "\n";
  writeFileSync(file, prefix.repeat(3000), "utf8");
  let expected = prefix.repeat(3000);
  try {
    for (const value of ["one", "two"]) {
      const recorder = new TraceRecorder("secret", file);
      recorder.record("event", { value, api_key: "secret" });
      recorder.timing("provider-end", {
        turn: 1,
        duration_ms: 3,
        event_count: 2,
      });
      expected +=
        JSON.stringify({
          kind: "event",
          payload: { value, api_key: "[REDACTED]" },
        }) + "\n";
      const receipt = await recorder.flush();
      expect(readFileSync(file, "utf8")).toBe(expected);
      expect(receipt?.sha256).toBe(
        createHash("sha256").update(expected).digest("hex"),
      );
    }
    const timing = readFileSync(`${file}.timing.jsonl`, "utf8");
    expect(timing).not.toContain("secret");
    expect(timing.trim().split("\n")).toHaveLength(2);
    expect(JSON.parse(timing.split("\n")[0]!)).toMatchObject({
      phase: "provider-end",
      duration_ms: 3,
    });
  } finally {
    rmSync(directory, { force: true, recursive: true });
  }
});

test("optional timing open errors do not prevent a durable primary receipt", async () => {
  const directory = mkdtempSync(path.join(tmpdir(), "ceo_arena-timing-open-"));
  const file = path.join(directory, "trace.ndjson");
  mkdirSync(`${file}.timing.jsonl`);
  try {
    const recorder = new TraceRecorder("secret", file);
    recorder.record("sealed", {});
    recorder.timing("provider-end");
    const receipt = await recorder.flush();
    expect(recorder.timingErrorCount).toBe(1);
    expect(receipt?.sha256).toBe(
      createHash("sha256").update(readFileSync(file)).digest("hex"),
    );
  } finally {
    rmSync(directory, { force: true, recursive: true });
  }
});

for (const phase of ["append", "flush"] as const) {
  test(`optional timing ${phase} errors leave the primary trace authoritative`, async () => {
    const directory = mkdtempSync(
      path.join(tmpdir(), "ceo_arena-timing-fault-"),
    );
    const file = path.join(directory, "trace.ndjson");
    const recorder = new TraceRecorder("secret", file);
    const originalWrite = fs.writeFileSync;
    const originalSync = fs.fsyncSync;
    let calls = 0;
    const fault =
      phase === "append"
        ? spyOn(fs, "writeFileSync").mockImplementation(
            (...args: Parameters<typeof fs.writeFileSync>) => {
              if (++calls === 2) throw new Error("optional timing disk error");
              return originalWrite(...args);
            },
          )
        : spyOn(fs, "fsyncSync").mockImplementation((descriptor: number) => {
            if (++calls === 2) throw new Error("optional timing fsync error");
            return originalSync(descriptor);
          });
    try {
      recorder.record("before", {});
      recorder.timing("provider-end");
      recorder.record("after", {});
      const receipt = await recorder.flush();
      expect(recorder.timingErrorCount).toBe(1);
      expect(receipt?.sha256).toBe(
        createHash("sha256").update(readFileSync(file)).digest("hex"),
      );
      expect(readFileSync(file, "utf8").trim().split("\n")).toHaveLength(2);
    } finally {
      fault.mockRestore();
      recorder.close();
      rmSync(directory, { force: true, recursive: true });
    }
  });
}

test("primary trace fsync failures remain fatal instead of returning an unaudited receipt", async () => {
  const directory = mkdtempSync(
    path.join(tmpdir(), "ceo_arena-primary-fault-"),
  );
  const recorder = new TraceRecorder(
    "secret",
    path.join(directory, "trace.ndjson"),
  );
  recorder.record("sealed", {});
  const fault = spyOn(fs, "fsyncSync").mockImplementation(() => {
    throw new Error("primary fsync failed");
  });
  try {
    await expect(recorder.flush()).rejects.toThrow("primary fsync failed");
  } finally {
    fault.mockRestore();
    recorder.close();
    rmSync(directory, { force: true, recursive: true });
  }
});
