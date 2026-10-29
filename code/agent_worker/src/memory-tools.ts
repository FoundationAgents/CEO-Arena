import { Tool, ToolFailure } from "@opencode-ai/llm";
import { Effect, Schema } from "effect";

import { CompanyWorkspace, storageKey, WorkspaceError } from "./memory";

const PositiveInt = Schema.Int.check(Schema.isGreaterThan(0));
const INPUT_FILE_ERRORS = new Set([
  "ENOENT",
  "ENAMETOOLONG",
  "EISDIR",
  "ENOTDIR",
  "EEXIST",
  "EINVAL",
]);
const action = (operation: () => string) =>
  Effect.try({
    try: operation,
    catch: (error) => error,
  }).pipe(
    Effect.catch((error) => {
      const code =
        typeof error === "object" && error && "code" in error
          ? error.code
          : undefined;
      const message =
        error instanceof WorkspaceError
          ? error.message
          : typeof code === "string" && INPUT_FILE_ERRORS.has(code)
            ? `File operation rejected (${code}); check the relative path and file type.`
            : undefined;
      return message
        ? Effect.fail(new ToolFailure({ message, error: { category: "tool" } }))
        : Effect.die(error);
    }),
  );

/** File tools affect only durable private memory, never an economic draft. */
export const createMemoryTools = (
  workspace: CompanyWorkspace,
  operationID: () => string,
) => ({
  read_file: Tool.make({
    description:
      "Read a UTF-8 file in your private persistent workspace with line numbers. offset is one-based; limit is optional. MEMORY.md is automatically loaded each week, up to its first 40,000 characters; read further here when needed.",
    parameters: Schema.Struct({
      path: Schema.String,
      offset: Schema.optional(PositiveInt),
      limit: Schema.optional(PositiveInt),
    }),
    success: Schema.String,
    execute: ({ path, offset, limit }) =>
      action(() => workspace.read(path, offset, limit)),
  }),
  write_file: Tool.make({
    description:
      "Create or overwrite a private workspace file. Parent directories are created. Writes persist immediately, including before seal_decision and across reset_decision, budget exhaustion and new weeks. No script execution occurs.",
    parameters: Schema.Struct({ path: Schema.String, content: Schema.String }),
    success: Schema.String,
    execute: (input, context) =>
      action(() =>
        workspace.write(
          input.path,
          input.content,
          `${operationID()}:${context?.id ?? "direct"}:${storageKey(JSON.stringify(input))}`,
        ),
      ),
  }),
  edit_file: Tool.make({
    description:
      "Replace old_string with new_string exactly once in a private file. The old text must be nonempty and unique. The edit is immediately persistent and independent of operating decisions.",
    parameters: Schema.Struct({
      path: Schema.String,
      old_string: Schema.String,
      new_string: Schema.String,
    }),
    success: Schema.String,
    execute: (input, context) =>
      action(() =>
        workspace.edit(
          input.path,
          input.old_string,
          input.new_string,
          `${operationID()}:${context?.id ?? "direct"}:${storageKey(JSON.stringify(input))}`,
        ),
      ),
  }),
  search_files: Tool.make({
    description:
      "Search private text files using a regular expression. Optional path and glob narrow the search. At most 100 files are searched and 200 matching lines returned; no other company or runtime files are accessible.",
    parameters: Schema.Struct({
      pattern: Schema.String,
      path: Schema.optional(Schema.String),
      glob: Schema.optional(Schema.String),
    }),
    success: Schema.String,
    execute: ({ pattern, path, glob }) =>
      action(() => workspace.search(pattern, path, glob)),
  }),
  glob_files: Tool.make({
    description:
      "List up to 200 matching paths in your private persistent workspace using a relative glob pattern. Does not access repository, credentials, other companies or internal snapshots.",
    parameters: Schema.Struct({ pattern: Schema.String }),
    success: Schema.String,
    execute: ({ pattern }) => action(() => workspace.glob(pattern)),
  }),
});
