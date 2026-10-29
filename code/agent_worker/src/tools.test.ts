import { expect, test } from "bun:test";

import { Tool } from "@opencode-ai/llm";
import { Schema } from "effect";

import { AgentToolGateway } from "./draft";
import { decisionFixture } from "./testing";
import { createTools, TOOL_DESCRIPTIONS } from "./tools";

for (const name of [
  "preview_decision",
  "reset_decision",
  "seal_decision",
] as const) {
  test(name + " requires an empty object and exports no root union", () => {
    const tools = createTools(new AgentToolGateway(decisionFixture().request), {
      query: async () => ({ columns: [], rows: [], truncated: false }),
      validate: async () => {},
    });
    const definition = Tool.toDefinitions(tools).find(
      (tool) => tool.name === name,
    );
    expect(definition?.inputSchema).toEqual({
      type: "object",
      additionalProperties: false,
    });
    const decode = Schema.decodeUnknownSync(tools[name].parameters);
    expect(decode({})).toEqual({});
    for (const input of [[], null, "", { company_id: "other" }])
      expect(() => decode(input)).toThrow();
  });
}

test("tool descriptions distinguish persistent policies from one-time actions", () => {
  expect(TOOL_DESCRIPTIONS.set_product).toContain(
    "set the development budget to 0",
  );
  expect(TOOL_DESCRIPTIONS.set_marketing).toContain("until explicitly changed");
  expect(TOOL_DESCRIPTIONS.set_operations).toContain(
    "charged every simulated day",
  );
  expect(TOOL_DESCRIPTIONS.start_research_project).toContain(
    "company-wide product-quality innovation",
  );
  expect(TOOL_DESCRIPTIONS.start_research_project).toContain(
    "not consumer market research",
  );
  expect(TOOL_DESCRIPTIONS.request_market_research).toContain("sampling date");
  expect(TOOL_DESCRIPTIONS.expand_capacity).toContain("operating cost");
  expect(TOOL_DESCRIPTIONS.seal_decision).toContain(
    "existing recurring policies continue",
  );
});

test("capacity and cash tools distinguish reversible work from sunk costs", () => {
  expect(TOOL_DESCRIPTIONS.downsize_capacity).toContain("not refunded");
  expect(TOOL_DESCRIPTIONS.cancel_capacity_project).toContain(
    "never completes",
  );
  expect(TOOL_DESCRIPTIONS.preview_decision).toContain(
    "not a profit or solvency forecast",
  );
});
