import { randomUUID } from "node:crypto";
import {
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";

export class PersistenceError extends Error {}

/** Publish a complete local file; never expose a partially written snapshot. */
export function writeAtomic(file: string, content: string): void {
  mkdirSync(path.dirname(file), { recursive: true });
  const temporary = path.join(
    path.dirname(file),
    `.ceo-arena-${randomUUID()}.tmp`,
  );
  const descriptor = openSync(temporary, "wx");
  try {
    try {
      writeFileSync(descriptor, content, "utf8");
      fsyncSync(descriptor);
    } finally {
      closeSync(descriptor);
    }
    renameSync(temporary, file);
  } finally {
    if (existsSync(temporary)) unlinkSync(temporary);
  }
}

/** Typed persistence shared by conversation checkpoints and file-operation receipts. */
export class AtomicDocument<Value> {
  constructor(
    readonly file: string,
    private readonly decode: (json: string) => Value,
  ) {}

  load(): Value | undefined {
    if (!existsSync(this.file)) return;
    try {
      return this.decode(readFileSync(this.file, "utf8"));
    } catch {
      throw new PersistenceError(
        "Stored agent state is unreadable or invalid; refusing to reset it.",
      );
    }
  }

  save(value: Value): void {
    try {
      writeAtomic(this.file, JSON.stringify(value));
    } catch {
      throw new PersistenceError("Agent state could not be saved durably.");
    }
  }
}
