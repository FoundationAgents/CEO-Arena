import path from "node:path";

import { Message, SystemPart, ToolCallPart } from "@opencode-ai/llm";
import { Schema } from "effect";

import { CompanyWorkspace, storageKey } from "./memory";
import {
  AgentUsageSchema,
  DECISION_PROTOCOL_VERSION,
  DecisionDiagnosticsSchema,
} from "./protocol";
import { AtomicDocument, PersistenceError } from "./storage";
import { CompanyDecisionSchema } from "./tools";

// Preserve the native AST while narrowing its upstream service-free codec type.
const NativeMessage = Schema.make<Schema.Codec<Message, typeof Message.Encoded>>(Message.ast);

export const PendingTurnSchema = Schema.Struct({
  message: NativeMessage,
  calls: Schema.Array(ToolCallPart),
  results: Schema.Array(NativeMessage),
  closing_notice: Schema.optional(Schema.Boolean),
});
export type PendingTurn = typeof PendingTurnSchema.Type;

const ConversationSnapshot = Schema.Struct({
  protocol: Schema.Literal(DECISION_PROTOCOL_VERSION),
  world_version: Schema.Number,
  binding: Schema.String,
  system: Schema.Array(SystemPart),
  messages: Schema.Array(NativeMessage),
  draft: CompanyDecisionSchema,
  usage: AgentUsageSchema,
  diagnostics: DecisionDiagnosticsSchema,
  closing: Schema.Boolean,
  pending: Schema.optional(PendingTurnSchema),
  status: Schema.optional(Schema.Literals(["sealed", "budget_exhausted"])),
  error: Schema.optional(Schema.String),
});
export type ConversationSnapshot = typeof ConversationSnapshot.Type;

/** Restore native messages and durable tool receipts only for the exact same window. */
export class ConversationJournal {
  private readonly document: AtomicDocument<ConversationSnapshot>;

  constructor(
    workspace: CompanyWorkspace,
    private readonly version: number,
    private readonly binding: string,
  ) {
    this.document = new AtomicDocument(
      path.join(workspace.internal, "conversation.json"),
      Schema.decodeUnknownSync(Schema.fromJsonString(ConversationSnapshot)),
    );
  }

  load(): ConversationSnapshot | undefined {
    const snapshot = this.document.load();
    if (snapshot && snapshot.world_version < this.version) return;
    if (
      snapshot &&
      (snapshot.world_version !== this.version ||
        snapshot.binding !== this.binding)
    )
      throw new PersistenceError(
        "Conversation checkpoint differs from the frozen window; refusing to mix protocols, models or observations.",
      );
    const pending = snapshot?.pending;
    if (
      pending &&
      (pending.results.length > pending.calls.length ||
        !pending.results.every((message, index) => {
          const part = message.content[0];
          const call = pending.calls[index];
          return (
            message.role === "tool" &&
            message.content.length === 1 &&
            part?.type === "tool-result" &&
            part.id === call?.id &&
            part.name === call.name
          );
        }))
    )
      throw new PersistenceError(
        "Saved tool receipts do not match the pending response; refusing to replay it.",
      );
    return snapshot;
  }

  save(
    snapshot: Omit<
      ConversationSnapshot,
      "binding" | "protocol" | "world_version"
    >,
  ): void {
    this.document.save({
      ...snapshot,
      binding: this.binding,
      protocol: DECISION_PROTOCOL_VERSION,
      world_version: this.version,
    });
  }

  static binding(identity: unknown): string {
    return storageKey(JSON.stringify(identity));
  }
}
