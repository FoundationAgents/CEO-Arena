import { expect, test } from "bun:test";

import {
  normalizeOpenAiChatSseLine,
  normalizeOpenAiChatSseStream,
} from "./compatibility";

test("preserves the initial tool identity", () => {
  const line =
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"query_company","arguments":""}}]}}]}';

  expect(normalizeOpenAiChatSseLine(line)).toContain('"id":"call_1"');
  expect(normalizeOpenAiChatSseLine(line)).toContain('"name":"query_company"');
});

test("drops empty continuation identity while preserving arguments", () => {
  const line =
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"","function":{"name":"","arguments":"{}"}}]}}]}';
  const normalized = normalizeOpenAiChatSseLine(line);

  expect(normalized).not.toContain('"id":""');
  expect(normalized).not.toContain('"name":""');
  expect(normalized).toContain('"arguments":"{}"');
});

test("leaves non-data and malformed lines untouched", () => {
  expect(normalizeOpenAiChatSseLine(": keepalive")).toBe(": keepalive");
  expect(normalizeOpenAiChatSseLine("data: not-json")).toBe("data: not-json");
});

test("normalizes continuation metadata across arbitrary byte chunks", async () => {
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();
  const source = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        encoder.encode(
          'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"query_company","arguments":""}}]}}]}\r\n',
        ),
      );
      controller.enqueue(
        encoder.encode(
          'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"na',
        ),
      );
      controller.enqueue(
        encoder.encode(
          'me":"","arguments":"{}"}}]}}]}\r\ndata: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2}}\r\n\r\n',
        ),
      );
      controller.close();
    },
  });

  const output = decoder.decode(
    await new Response(normalizeOpenAiChatSseStream(source)).arrayBuffer(),
  );

  expect(output).toContain('"id":"call_1"');
  expect(output).not.toContain('"name":""');
  expect(output).toContain('"arguments":"{}"');
  expect(output).toContain('"usage":{"prompt_tokens":4,"completion_tokens":2}');
});
