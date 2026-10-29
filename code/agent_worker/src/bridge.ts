import type { CompanyDecision, QueryResult } from "./protocol";

export class ToolRejectedError extends Error {}

export interface CompanyBridge {
  query(input: {
    readonly sql: string;
    readonly parameters: ReadonlyArray<string | number | null>;
    readonly rowLimit: number;
  }): Promise<QueryResult>;
  validate(decision: CompanyDecision): Promise<void>;
}

class PendingCalls<Value> {
  private readonly pending = new Map<
    string,
    {
      resolve: (value: Value) => void;
      reject: (error: Error) => void;
    }
  >();

  request(id: string, send: () => void): Promise<Value> {
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      try {
        send();
      } catch (error) {
        this.pending.delete(id);
        reject(error);
      }
    });
  }

  resolve(id: string, value: Value, error?: string | null) {
    const pending = this.pending.get(id);
    if (!pending) throw new Error("uncorrelated company RPC response");
    this.pending.delete(id);
    if (error) pending.reject(new ToolRejectedError(error));
    else pending.resolve(value);
  }

  close() {
    for (const pending of this.pending.values())
      pending.reject(new Error("decision window closed"));
    this.pending.clear();
  }
}

/** One company-scoped channel for reads and side-effect-free draft validation. */
export class RpcCompanyBridge implements CompanyBridge {
  private sequence = 0;
  private readonly queries = new PendingCalls<QueryResult>();
  private readonly validations = new PendingCalls<void>();

  constructor(
    private readonly requestID: string,
    private readonly send: (message: object) => void,
  ) {}

  query(input: Parameters<CompanyBridge["query"]>[0]) {
    const id = `q${++this.sequence}`;
    return this.queries.request(id, () =>
      this.send({
        type: "query",
        request_id: this.requestID,
        query_id: id,
        sql: input.sql,
        parameters: input.parameters,
        row_limit: input.rowLimit,
      }),
    );
  }

  validate(decision: CompanyDecision) {
    const id = `v${++this.sequence}`;
    return this.validations.request(id, () =>
      this.send({
        type: "validate_decision",
        request_id: this.requestID,
        validation_id: id,
        decision,
      }),
    );
  }

  resolveQuery(id: string, result?: QueryResult, error?: string | null) {
    if (!result && !error) throw new Error("query response omitted its result");
    this.queries.resolve(
      id,
      result ?? { columns: [], rows: [], truncated: false },
      error,
    );
  }

  resolveValidation(id: string, error?: string | null) {
    this.validations.resolve(id, undefined, error);
  }

  close() {
    this.queries.close();
    this.validations.close();
  }
}
