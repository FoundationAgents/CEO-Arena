import {
  LLMClient,
  type LLMEvent,
  LLMRequest,
  LLMResponse,
  Message,
  ToolRuntime,
  mergeGenerationOptions,
  toDefinitions,
  type ToolCallPart,
  type ToolResultValue,
} from "@opencode-ai/llm";
import { Cause, Effect, Exit, Option, Stream } from "effect";

import type { CompanyBridge } from "./bridge";
import { DecisionBriefing } from "./briefing";
import { DecisionBudget } from "./budget";
import { TruncatedContinuation } from "./continuation";
import {
  ConversationJournal,
  type ConversationSnapshot,
  type PendingTurn,
} from "./conversation";
import { AgentToolGateway } from "./draft";
import { describeFailure, ProviderFault, TransportRecovery } from "./failure";
import { CompanyWorkspace, MEMORY_INSTRUCTIONS } from "./memory";
import { createMemoryTools } from "./memory-tools";
import {
  DECISION_PROTOCOL_VERSION,
  type AgentUsage,
  type DecisionDiagnostics,
  type DecisionRequest,
  type DecisionResult,
  type ProviderFailure,
} from "./protocol";
import { NewApiModel, type ProviderConfig } from "./provider";
import { invalidToolInput } from "./recovery";
import { createTools } from "./tools";
import { TraceRecorder } from "./trace";
import { combineUsage, EMPTY_USAGE } from "./usage";

export const SYSTEM_PROMPT = [
  "You are the CEO of one company in a competitive standardized-product market.",
  "",
  [
    "You see only your company's private read-only database and public market information.",
    "You cannot access or claim hidden competitor state; base estimates only on public and own-company evidence.",
    "Use query_company for evidence, then stage decisions with the provided tools.",
    "The database is a fixed snapshot for this decision window. Repeating a query will not create new records.",
    "Historical operating records end at the current day; after empty results, use available evidence rather than searching future operating history. Known future shipment or project dates remain valid information.",
    "Staging never mutates the world.",
    "A staged receipt confirms a draft change, not execution; inspect action_receipts after settlement for actual charges, refunds, outcomes, and effective dates.",
    "The company identity and world version are bound by the Harness and are intentionally absent from tool arguments.",
  ].join(" "),
  [
    "Price, promotion, reorder target, daily development budget, each segment-channel marketing budget, and the reliability and support budgets persist across decision windows until explicitly changed.",
    "Marketing, development, reliability, support, and the active capacity tier's operating cost are charged every simulated day.",
    "Set a recurring budget to 0 to stop it.",
    "Sealing a fresh draft with no staged operating changes preserves all current recurring policies; one-time projects, bids, and market-research requests are not automatically repeated.",
  ].join(" "),
  "",
  [
    "`start_research_project` makes a one-time, delayed, company-wide product-quality innovation investment that adds product knowledge capital after completion.",
    "Its quoted expected knowledge gain is the company-wide total, divided evenly across product lines, not the gain for each product. An applied project receipt confirms placement, not completion or realized quality gain.",
    "`request_market_research` makes a one-time purchase of a delayed, noisy private report about a customer segment's demand, preferences, and quality requirements; it does not improve product quality. Check its sampling date because market conditions can change before delivery.",
    "`expand_capacity` builds additional capacity after a delay. `downsize_capacity` reduces installed capacity after a delay and an adjustment fee; it does not refund past construction spending or remove outstanding orders.",
    "`cancel_capacity_project` stops unfinished construction and refunds only the unspent prepaid amount minus cancellation costs, never sunk costs; check capacity_action_quotes before committing.",
    "Use daily_cashflow and daily_expenses to distinguish revenue, procurement, recurring operating costs, investments, and refunds. Use preview_decision before sealing to inspect combined upfront commitments and daily policy costs. Compare investment payback with days_remaining; projects, knowledge, and capacity have no separate terminal score value.",
    "Cash below zero causes bankruptcy. Combined one-time commitments must fit currently observable funds; this check does not guarantee future daily costs or procurement are affordable.",
    "Use reset_decision to discard unwanted unsealed commitments and re-stage a plan; it does not reset consumed decision budgets or change existing world policies.",
  ].join(" "),
  "",
  "Your sole objective is to maximize terminal cash plus terminal inventory liquidation value minus initial enterprise value.",
  "",
  MEMORY_INSTRUCTIONS,
  "",
  [
    "You must finish every decision window by calling seal_decision as the sole tool call of its provider turn.",
    "Recoverable errors are returned for correction in the same conversation and draft; earlier work and usage accounting are preserved.",
    "Failed actions never change the draft, and earlier successfully staged changes are preserved.",
    "A response mixing seal_decision with other calls is rejected in full; resend those calls separately.",
    "Sealing validates the draft before accepting it. Correct any validation error and seal again.",
    "All model replies and attempted tool calls, including rejected calls, are recorded. The standard window allowance is 32 decision replies, 131072 cumulative generated tokens (reasoning output included, not counted twice), and 3600 seconds of active execution.",
    "Finalization begins at 28 decision replies or 75 percent of the token or time allowance, whichever comes first. At most four further decision replies may complete the draft with query_company disabled; all hard limits still apply. Provider-native single-response limits still apply. Explicit window limits below override the standard allowance.",
    "If no valid seal is produced within the allowance, the unsealed draft is discarded, prior operating policies continue for this window; already written private files remain saved, and the outcome is recorded as budget_exhausted, not a successful hold decision.",
    "The current week keeps its conversation and tool results without an automatic summary. The next week starts fresh with MEMORY.md and a new factual briefing. There is no default cumulative tool-call cap.",
    "A provider may limit a single response. If that response is cut short, continue in this same conversation and finish with a valid seal; a truncated response never seals automatically.",
    "Do not call tools after sealing.",
  ].join(" "),
].join("\n");

export type { ProviderConfig } from "./provider";
export const remainingGeneration = (request: LLMRequest, maxTokens?: number) =>
  maxTokens === undefined
    ? request.generation
    : mergeGenerationOptions(request.generation, {
        maxTokens: Math.min(
          maxTokens,
          request.generation?.maxTokens ?? Infinity,
        ),
      });

export interface DecisionSessionOptions {
  readonly workspace?: CompanyWorkspace;
}

class FinalizationRequired extends Error {}
const FINALIZATION_INSTRUCTIONS =
  "FINALIZATION: query_company is disabled. Use the existing evidence, complete operating decisions and save memory if useful, then seal alone. No extra decision budget is granted.";

/** Own one recoverable weekly conversation; file memory is independent of its economic draft. */
export class DecisionSession {
  private readonly gateway: AgentToolGateway;
  private readonly tools: ReturnType<typeof createTools> &
    ReturnType<typeof createMemoryTools>;
  private readonly budget: DecisionBudget;
  private readonly workspace: CompanyWorkspace;
  private readonly ownsWorkspace: boolean;
  private readonly journal: ConversationJournal;
  private readonly continuation: TruncatedContinuation;
  private readonly restored?: ConversationSnapshot;
  private readonly baseline: AgentUsage;
  private readonly baselineDiagnostics?: DecisionDiagnostics;
  private readonly generation: LLMRequest["generation"];
  private readonly maxResponseTokens?: number;
  private request: LLMRequest;
  private pending?: PendingTurn;
  private closing = false;
  private budgetUsageIncomplete = false;
  private protocolErrors = 0;
  private toolErrors = 0;
  private transportFailure?: ProviderFailure;

  constructor(
    message: DecisionRequest,
    model: NewApiModel,
    bridge: CompanyBridge,
    private readonly trace: TraceRecorder,
    private readonly recovery = new TransportRecovery(),
    options: DecisionSessionOptions = {},
  ) {
    const issued = message.request;
    this.continuation = new TruncatedContinuation(trace);
    this.ownsWorkspace = options.workspace === undefined;
    this.workspace =
      options.workspace ??
      CompanyWorkspace.ephemeral(issued.run_id, issued.company_id);
    this.baseline = issued.prior_usage ?? EMPTY_USAGE;
    this.baselineDiagnostics = issued.prior_diagnostics;
    this.journal = new ConversationJournal(
      this.workspace,
      issued.world_version,
      ConversationJournal.binding({
        protocol: DECISION_PROTOCOL_VERSION,
        model: model.id,
        route: model.protocol,
        inference: model.inference,
        run: issued.run_id,
        company: issued.company_id,
        observation: issued.observation,
        limits: issued.budget,
      }),
    );
    this.restored = this.journal.load();
    const consumed = combineUsage(
      this.baseline,
      this.restored?.usage ?? EMPTY_USAGE,
      Math.max,
    );
    this.budget = new DecisionBudget(issued.budget, consumed);
    this.gateway = new AgentToolGateway(issued);
    this.pending = this.restored?.pending;
    this.closing = this.restored?.closing ?? false;
    if (this.closing) this.budget.enterClosing();
    this.protocolErrors = Math.max(
      this.baselineDiagnostics?.protocol_errors ?? 0,
      this.restored?.diagnostics.protocol_errors ?? 0,
    );
    this.toolErrors = Math.max(
      this.baselineDiagnostics?.tool_errors ?? 0,
      this.restored?.diagnostics.tool_errors ?? 0,
    );
    this.budgetUsageIncomplete = !!(
      this.baselineDiagnostics?.budget_usage_incomplete ||
      this.restored?.diagnostics.budget_usage_incomplete
    );
    if (this.restored) this.gateway.draft.restore(this.restored.draft);
    this.tools = {
      ...createTools(this.gateway, bridge),
      ...createMemoryTools(
        this.workspace,
        () =>
          `${issued.world_version}:${this.budget.total.provider_turns}:${this.pending?.results.length ?? 0}`,
      ),
    };
    const prompt = [
      `Decision window ${issued.world_version} on day ${issued.observation.day}.`,
      `The simulation ends on day ${issued.observation.total_days}; ${issued.observation.days_remaining} days remain.`,
      "Query your authorized database, stage a coherent decision, and seal it.",
      new DecisionBriefing(issued.observation).render(),
      "Window limits: " + JSON.stringify(issued.budget),
      "Previously consumed allowance in this same window (never renewed): " +
        JSON.stringify(consumed),
    ].join("\n");
    this.request = model.request({
      system: SYSTEM_PROMPT + this.workspace.memory(),
      prompt,
      tools: toDefinitions(this.tools),
    });
    this.generation = this.request.generation;
    this.maxResponseTokens = model.singleResponseLimit;
    if (this.restored)
      this.request = LLMRequest.update(this.request, {
        system: this.restored.system,
        messages: this.continuation.restore(this.restored.messages),
      });
    trace.record("decision-start", {
      request_id: message.request_id,
      company_id: issued.company_id,
      world_version: issued.world_version,
      model: model.id,
      ...issued.budget,
      prior_usage: consumed,
      restored: !!this.restored,
      pending_tool_results: this.pending?.results.length ?? 0,
    });
    trace.timing("decision-start");
    trace.record("request-contract", {
      decision_protocol_version: DECISION_PROTOCOL_VERSION,
      system: this.request.system,
      messages: this.request.messages,
      tools: this.request.tools,
      generation: this.request.generation,
      protocol: model.protocol,
      inference: model.inference,
      tool_choice: this.request.toolChoice,
      provider_options: this.request.model.route.defaults.providerOptions,
    });
  }

  run() {
    const session = this;
    return Effect.gen(function* () {
      if (session.restored?.status)
        return session.finish(session.restored.status, session.restored.error);
      session.checkpoint(); // Detect unavailable persistence before buying a model response.
      while (session.pending || !session.budget.exhausted) {
        if (session.budget.remainingMs === 0) return session.exhausted();
        const wasClosing = session.budget.closing;
        const deadline = session.budget.phaseMs;
        const step = session.step();
        const outcome = yield* Effect.exit(
          deadline === undefined
            ? step.pipe(Effect.map(Option.some))
            : step.pipe(Effect.timeoutOption(deadline)),
        );
        if (Exit.isFailure(outcome)) {
          if (Cause.hasInterrupts(outcome.cause))
            return yield* Effect.failCause(outcome.cause);
          if (Cause.squash(outcome.cause) instanceof FinalizationRequired)
            continue;
          const failure = describeFailure(outcome.cause);
          session.trace.record("provider-failure", failure);
          return session.finish("provider_error", failure.message, failure);
        }
        if (Option.isNone(outcome.value)) {
          session.trace.record("decision-deadline", {
            phase: wasClosing ? "finalization" : "analysis",
            usage: session.budget.total,
            draft_preserved: true,
          });
          if (wasClosing)
            return session.transportFailure
              ? session.finish(
                  "provider_error",
                  session.transportFailure.message,
                  session.transportFailure,
                )
              : session.finish(
                  "budget_exhausted",
                  "active-time budget exhausted before sealing",
                );
          session.budget.enterClosing();
          session.prepareFinalization();
          continue;
        }
        if (outcome.value.value) return outcome.value.value;
      }
      return session.exhausted();
    }).pipe(
      Effect.onExit((exit) =>
        Effect.sync(() => {
          if (Exit.isFailure(exit)) session.checkpoint();
        }),
      ),
      Effect.ensuring(
        Effect.sync(() => {
          if (session.ownsWorkspace) session.workspace.close();
        }),
      ),
    );
  }

  private step() {
    const session = this;
    return Effect.gen(function* () {
      if (session.pending) return yield* session.executePending();
      session.prepareFinalization();
      if (session.budget.exhausted) return session.exhausted();
      session.request = session.boundedRequest(session.request);
      const turn = session.budget.total.provider_turns;
      session.trace.record("provider-turn-request", {
        turn,
        messages: session.request.messages,
      });
      const response = yield* session.receive(turn);
      const calls = response.toolCalls.filter((call) => !call.providerExecuted);
      const accountingError = session.budget.record(
        response.usage,
        calls.length,
      );
      session.trace.timing("provider-usage", {
        purpose: "decision",
        turn,
        input_tokens: response.usage?.inputTokens,
        output_tokens: response.usage?.outputTokens,
        tool_calls: calls.length,
      });
      if (accountingError) {
        session.budgetUsageIncomplete = true;
        return session.finish("provider_error", accountingError, {
          category: "accounting",
          message: accountingError,
          retryable: false,
        });
      }
      if (session.budget.exceeded)
        return session.finish("budget_exhausted", session.budget.exceeded);
      if (session.budget.remainingMs === 0) return session.exhausted();
      const malformed = calls.filter(
        (call) => invalidToolInput(call) !== undefined,
      );
      if (malformed.length) {
        for (const call of malformed)
          session.error(
            "protocol",
            "invalid_tool_json",
            "Invalid JSON arguments for " + call.name,
            call.id,
          );
        session.continueWith([
          Message.user(
            [
              "Your last response contained invalid tool JSON; no calls from that response were executed. Earlier staged changes are preserved.",
              ...malformed.map(
                (call) =>
                  call.name +
                  ": invalid JSON " +
                  JSON.stringify(invalidToolInput(call)?.slice(0, 400)),
              ),
              "Re-emit valid JSON arguments, then call seal_decision alone.",
            ].join("\n"),
          ),
        ]);
        return undefined;
      }
      if (response.finishReason === "length") {
        session.continueWith(session.continuation.after(response));
        return undefined;
      }
      if (!calls.length) {
        const feedback =
          "No tool was called. Continue using the supplied tools; if your draft is ready, call seal_decision alone. Earlier staged changes are preserved.";
        session.error("protocol", "missing_tool_call", feedback);
        session.continueWith([response.message, Message.user(feedback)]);
        return undefined;
      }
      if (
        calls.some((call) => call.name === "seal_decision") &&
        calls.length !== 1
      ) {
        session.error(
          "protocol",
          "mixed_seal",
          "seal_decision must be the sole tool call of its provider turn",
        );
        session.continueWith([
          response.message,
          ...calls.map((call) =>
            Message.tool({
              id: call.id,
              name: call.name,
              result: {
                type: "error",
                value:
                  "No call in this response was executed. Resend ordinary calls, then seal_decision in a separate turn. Earlier staged changes are preserved.",
              },
            }),
          ),
        ]);
        return undefined;
      }
      session.pending = { message: response.message, calls, results: [] };
      session.checkpoint(); // Save paid response and allowance before executing any of its tools.
      return yield* session.executePending();
    });
  }

  private executePending() {
    const session = this;
    return Effect.gen(function* () {
      while (
        session.pending &&
        session.pending.results.length < session.pending.calls.length
      ) {
        const call = session.pending.calls[session.pending.results.length]!;
        const value = yield* session.dispatch(call);
        session.pending = {
          ...session.pending,
          results: [
            ...session.pending.results,
            Message.tool({ id: call.id, name: call.name, result: value }),
          ],
        };
        session.checkpoint(); // Completed siblings are never re-executed on recovery.
      }
      const completed = session.pending!;
      session.pending = undefined;
      const sealed =
        session.gateway.draft.sealed ||
        (completed.calls.length === 1 &&
          completed.calls[0]?.name === "seal_decision" &&
          completed.results[0]?.content.some(
            (part) =>
              part.type === "tool-result" && part.result.type !== "error",
          ));
      session.continueWith(
        [
          completed.message,
          ...completed.results,
          ...(completed.closing_notice && !sealed
            ? [Message.user(FINALIZATION_INSTRUCTIONS)]
            : []),
        ],
        sealed ? "sealed" : undefined,
      );
      if (sealed) return session.finish("sealed");
      return undefined;
    });
  }

  private dispatch(call: ToolCallPart) {
    const session = this;
    return Effect.gen(function* () {
      if (session.closing && call.name === "query_company") {
        const feedback =
          "query_company is disabled during finalization. Use existing evidence, update memory if useful, preview the draft and seal alone.";
        if (!session.pending?.closing_notice)
          session.error(
            "protocol",
            "query_during_finalization",
            feedback,
            call.id,
          );
        return { type: "error" as const, value: feedback };
      }
      const started = performance.now();
      const settled = yield* ToolRuntime.dispatch(session.tools, call);
      session.trace.timing("tool-end", {
        turn: session.budget.total.provider_turns - 1,
        tool: call.name in session.tools ? call.name : "unknown",
        duration_ms: performance.now() - started,
        status: settled.result.type,
      });
      settled.events.forEach((event) =>
        session.trace.record("tool-event", { event }),
      );
      if (settled.result.type === "error") {
        const domain = settled.events.some(
          (event) =>
            event.type === "tool-error" &&
            typeof event.error === "object" &&
            event.error !== null &&
            "category" in event.error &&
            event.error.category === "tool",
        );
        session.error(
          domain ? "tool" : "protocol",
          domain ? "tool_rejected" : "invalid_tool_call",
          String(settled.result.value),
          call.id,
        );
      }
      return settled.result;
    });
  }

  private receive(turn: number) {
    const session = this;
    return Effect.gen(function* () {
      for (let attempt = 1; ; attempt++) {
        const started = performance.now();
        let eventCount = 0;
        const usageEvents: LLMEvent[] = [];
        let accounted = false;
        const accountPartial = () => {
          if (accounted) return;
          accounted = true;
          const usage = LLMResponse.usage({ events: usageEvents });
          const issue = session.budget.record(usage, 0, true);
          session.budgetUsageIncomplete ||= !!issue;
          session.trace.timing("provider-usage", {
            turn,
            purpose: "decision",
            partial: true,
            input_tokens: usage?.inputTokens,
            output_tokens: usage?.outputTokens,
            usage_incomplete: !!issue,
          });
          session.checkpoint();
        };
        session.trace.timing("provider-start", {
          turn,
          attempt,
          purpose: "decision",
        });
        const received = yield* Effect.exit(
          LLMClient.stream(session.request).pipe(
            Stream.tap((event) =>
              Effect.sync(() => {
                usageEvents.push(event);
                if (eventCount++ === 0)
                  session.trace.timing("provider-first-event", {
                    turn,
                    attempt,
                    purpose: "decision",
                    duration_ms: performance.now() - started,
                  });
                session.trace.record("llm-event", {
                  turn,
                  purpose: "decision",
                  event,
                });
              }),
            ),
            Stream.runCollect,
            Effect.onExit((exit) =>
              Effect.sync(() => {
                if (Exit.isFailure(exit)) accountPartial();
              }),
            ),
          ),
        );
        session.trace.timing("provider-end", {
          turn,
          attempt,
          duration_ms: performance.now() - started,
          event_count: eventCount,
          purpose: "decision",
        });
        let failure: ProviderFailure;
        if (Exit.isFailure(received)) {
          if (Cause.hasInterrupts(received.cause))
            return yield* Effect.failCause(received.cause);
          failure = describeFailure(received.cause);
        } else {
          const events = Array.from(received.value);
          const response = LLMResponse.fromEvents(events);
          const providerError = events.find(
            (event) => event.type === "provider-error",
          );
          if (
            response?.finishReason &&
            response.finishReason !== "error" &&
            !providerError
          ) {
            session.transportFailure = undefined;
            return response;
          }
          failure = {
            category: "stream_read",
            message: "Provider stream ended without a complete response",
            retryable: providerError?.retryable ?? true,
            reason: providerError
              ? "ProviderErrorEvent"
              : "MissingTerminalResponse",
          };
          accountPartial();
        }
        failure = { ...failure, attempt };
        session.transportFailure = failure;
        if (!failure.retryable || session.budget.exhausted)
          return yield* Effect.fail(new ProviderFault(failure));
        if (session.budget.closing && !session.closing)
          return yield* Effect.fail(new FinalizationRequired());
        session.request = session.boundedRequest(session.request);
        const delay = session.recovery.delay(failure, attempt);
        session.trace.record("provider-retry", {
          turn,
          failure,
          delay_ms: delay,
          draft_preserved: true,
          usage: session.budget.total,
        });
        session.trace.timing("provider-retry", {
          turn,
          attempt,
          duration_ms: delay,
          failure,
        });
        yield* session.recovery.sleep(delay);
      }
    });
  }

  private prepareFinalization(): void {
    if (this.closing || !this.budget.closing) return;
    this.closing = true;
    // Signed thinking binds to the original prefix, including tool definitions.
    // Keep that prefix intact; dispatch rejects queries locally in closing.
    this.trace.record("finalization-start", {
      remaining_decision_turns: this.budget.turnsRemaining,
      remaining_output_tokens: this.budget.remainingTokens,
      remaining_active_ms: this.budget.remainingMs,
      allowed_tools: this.request.tools
        .filter((tool) => tool.name !== "query_company")
        .map((tool) => tool.name),
    });
    if (this.pending) {
      this.pending = { ...this.pending, closing_notice: true };
      this.checkpoint();
    } else this.continueWith([Message.user(FINALIZATION_INSTRUCTIONS)]);
  }

  private boundedRequest(request: LLMRequest): LLMRequest {
    const remaining = this.budget.phaseTokens;
    return LLMRequest.update(request, {
      generation: remainingGeneration(
        LLMRequest.update(request, { generation: this.generation }),
        remaining === undefined
          ? undefined
          : Math.min(remaining, this.maxResponseTokens ?? Infinity),
      ),
    });
  }

  private exhausted(): DecisionResult {
    return this.finish("budget_exhausted", this.budget.exhausted);
  }

  private continueWith(
    messages: ReadonlyArray<Message>,
    status?: "sealed",
  ): void {
    this.request = LLMRequest.update(this.request, {
      messages: [...this.request.messages, ...messages],
    });
    this.trace.record("decision-continued", {
      usage: this.budget.total,
      draft_preserved: true,
    });
    this.checkpoint(status);
  }

  private error(
    category: "protocol" | "tool",
    code: string,
    message: string,
    callID?: string,
  ): void {
    if (category === "protocol") this.protocolErrors++;
    else this.toolErrors++;
    this.trace.record("decision-error", {
      category,
      code,
      message,
      call_id: callID,
      turn: this.budget.total.provider_turns - 1,
      draft_preserved: true,
    });
  }

  private diagnostics(): DecisionDiagnostics {
    return {
      protocol_errors: this.protocolErrors,
      tool_errors: this.toolErrors,
      recovered: false,
      budget_usage_incomplete: this.budgetUsageIncomplete,
    };
  }

  private checkpoint(
    status?: "sealed" | "budget_exhausted",
    error?: string,
  ): void {
    this.journal.save({
      system: this.request.system,
      messages: this.request.messages,
      draft: this.gateway.draft.preview(),
      usage: this.budget.total,
      diagnostics: this.diagnostics(),
      closing: this.closing,
      pending: status ? undefined : this.pending,
      status,
      error,
    });
  }

  private finish(
    status: DecisionResult["status"],
    error?: string,
    failure?: ProviderFailure,
  ): DecisionResult {
    this.checkpoint(
      status === "sealed" || status === "budget_exhausted" ? status : undefined,
      error,
    );
    const result: DecisionResult = {
      status,
      error,
      failure,
      usage: combineUsage(this.budget.total, this.baseline, (a, b) =>
        Math.max(0, a - b),
      ),
      decision: status === "sealed" ? this.gateway.draft.seal() : undefined,
      diagnostics: {
        protocol_errors:
          this.protocolErrors -
          (this.baselineDiagnostics?.protocol_errors ?? 0),
        tool_errors:
          this.toolErrors - (this.baselineDiagnostics?.tool_errors ?? 0),
        recovered:
          status === "sealed" && this.protocolErrors + this.toolErrors > 0,
        budget_usage_incomplete: this.budgetUsageIncomplete,
      },
    };
    this.trace.record("decision-finished", result);
    this.trace.timing("decision-end", {
      status,
      output_tokens: result.usage.output_tokens,
      input_tokens: result.usage.input_tokens,
      tool_calls: result.usage.tool_calls,
      failure,
    });
    return result;
  }
}

export const runDecision = (
  message: DecisionRequest,
  provider: ProviderConfig,
  bridge: CompanyBridge,
  trace: TraceRecorder,
  recovery?: TransportRecovery,
  options?: DecisionSessionOptions,
) =>
  new DecisionSession(
    message,
    new NewApiModel({
      ...provider,
      onIdentity: (event) => {
        trace.record("provider-identity", event);
        provider.onIdentity?.(event);
      },
    }),
    bridge,
    trace,
    recovery,
    options,
  ).run();
