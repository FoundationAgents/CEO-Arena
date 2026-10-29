import { expect, test } from "bun:test";

import { AgentToolGateway, DecisionDraft } from "./draft";
import { decisionFixture } from "./testing";

test("capacity actions replace each other without ambiguous economic commitments", () => {
  const draft = new DecisionDraft("own", 1);
  draft.expandCapacity(2);
  draft.downsizeCapacity(0);
  expect(draft.preview()).toMatchObject({ capacity_downsize_tier: 0 });
  expect(draft.preview().capacity_target_tier).toBeUndefined();
  draft.cancelCapacityProject("project-1");
  expect(draft.preview().capacity_downsize_tier).toBeUndefined();
  expect(draft.seal().cancel_capacity_project_id).toBe("project-1");
  expect(() => draft.downsizeCapacity(0)).toThrow("already sealed");
});

test("gateway rejects lower-tier expansion and unknown cancellation before staging", () => {
  const gateway = new AgentToolGateway(decisionFixture().request);
  expect(() => gateway.expandCapacity(0)).toThrow("not currently available");
  expect(() => gateway.cancelCapacityProject("unknown")).toThrow(
    "not currently available",
  );
  expect(gateway.draft.preview().capacity_target_tier).toBeUndefined();
  gateway.downsizeCapacity(0);
  expect(gateway.preview().upfront_charges_cents).toBe(500);
});

test("preview combines upfront commitments and preserves an editable unaffordable draft", () => {
  const gateway = new AgentToolGateway(decisionFixture().request);
  gateway.expandCapacity(2);
  gateway.draft.startResearchProject(2);
  gateway.requestMarketResearch({ segment_id: "value", level: 1 });
  expect(gateway.preview()).toMatchObject({
    upfront_charges_cents: 11500,
    upfront_refunds_cents: 0,
    cash_after_upfront_cents: -1500,
    daily_policy_cost_cents: 400,
    affordable: false,
  });
  expect(() => gateway.seal()).toThrow("combined upfront commitments");
  expect(gateway.draft.sealed).toBe(false);
  gateway.draft.startResearchProject(1);
  expect(gateway.seal().research_project_tier).toBe(1);
});

test("preview uses authoritative cancellation refunds without inventing sunk-cost formulas", () => {
  const message = decisionFixture();
  const gateway = new AgentToolGateway({
    ...message.request,
    observation: {
      ...message.request.observation,
      capacity_action_quotes: [
        {
          action: "cancel_capacity_project",
          project_id: "project-1",
          target_tier: 2,
          charged_cents: 0,
          refunded_cents: 1234,
        },
      ],
    },
  });
  gateway.cancelCapacityProject("project-1");
  expect(gateway.preview().cash_after_upfront_cents).toBe(11234);
  expect(gateway.seal().cancel_capacity_project_id).toBe("project-1");
});

test("daily estimate merges policy patches, but excludes future capacity and purchases", () => {
  const gateway = new AgentToolGateway(decisionFixture().request);
  gateway.setProduct({
    product_id: "basic",
    development_budget_cents: 0,
    reorder_target: 9999,
  });
  gateway.setMarketing({
    segment_id: "value",
    channel: "search",
    daily_budget_cents: 0,
  });
  gateway.draft.setOperations(0, 10);
  gateway.expandCapacity(2);
  expect(gateway.preview().daily_policy_cost_cents).toBe(210);
  expect(gateway.preview().note).toContain("excludes procurement");
});

test("reset removes all unsealed commitments without altering existing operating policies", () => {
  const gateway = new AgentToolGateway(decisionFixture().request);
  gateway.expandCapacity(2);
  gateway.draft.startResearchProject(1);
  gateway.setProduct({ product_id: "basic", development_budget_cents: 9999 });
  gateway.draft.reset();
  expect(gateway.preview()).toMatchObject({
    upfront_charges_cents: 0,
    daily_policy_cost_cents: 400,
  });
  expect(gateway.seal().products).toEqual([]);
  expect(() => gateway.draft.reset()).toThrow("already sealed");
});

test("preview snapshots never alias staged or sealed decisions", () => {
  const gateway = new AgentToolGateway(decisionFixture().request);
  gateway.setProduct({ product_id: "basic", price_cents: 1500 });
  const before = gateway.draft.preview();
  Reflect.set(before.products, "length", 0);
  gateway.expandCapacity(2);
  expect(gateway.draft.preview().products[0]?.price_cents).toBe(1500);
  const sealed = gateway.seal();
  const after = gateway.draft.preview();
  Reflect.set(after.products, "length", 0);
  expect(sealed.products[0]?.price_cents).toBe(1500);
  expect(sealed.capacity_target_tier).toBe(2);
});
