const CHAT_COMPLETIONS_PATH = /\/chat\/completions(?:\?|$)/;

/** Normalize one NewAPI/OpenAI-compatible SSE line without touching content. */
export const normalizeOpenAiChatSseLine = (line: string) => {
  if (!line.startsWith("data: ") || line === "data: [DONE]") return line;
  try {
    const event = JSON.parse(line.slice(6)) as {
      choices?: Array<{
        delta?: {
          tool_calls?: Array<{
            id?: string;
            function?: { name?: string; arguments?: string };
          }>;
        };
      }>;
    };
    for (const choice of event.choices ?? []) {
      for (const tool of choice.delta?.tool_calls ?? []) {
        if (tool.id === "") delete tool.id;
        if (tool.function?.name === "") delete tool.function.name;
      }
    }
    return `data: ${JSON.stringify(event)}`;
  } catch {
    return line;
  }
};

export const normalizeOpenAiChatSseStream = (
  body: ReadableStream<Uint8Array>,
) => {
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  let buffer = "";
  return body.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        buffer += decoder.decode(chunk, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines)
          controller.enqueue(
            encoder.encode(`${normalizeOpenAiChatSseLine(line)}\n`),
          );
      },
      flush(controller) {
        buffer += decoder.decode();
        if (buffer)
          controller.enqueue(
            encoder.encode(normalizeOpenAiChatSseLine(buffer)),
          );
      },
    }),
  );
};

const requestUrl = (input: Parameters<typeof fetch>[0]) => {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
};

/** Install the isolated worker's narrow NewAPI streaming compatibility adapter. */
export const installNewApiStreamCompatibility = () => {
  const originalFetch = fetch;
  globalThis.fetch = (async (input, init) => {
    const response = await originalFetch(input, init);
    if (
      !response.body ||
      !CHAT_COMPLETIONS_PATH.test(requestUrl(input)) ||
      !response.headers.get("content-type")?.includes("text/event-stream")
    )
      return response;
    const headers = new Headers(response.headers);
    headers.delete("content-encoding");
    headers.delete("content-length");
    return new Response(normalizeOpenAiChatSseStream(response.body), {
      headers,
      status: response.status,
      statusText: response.statusText,
    });
  }) as typeof fetch;
};
