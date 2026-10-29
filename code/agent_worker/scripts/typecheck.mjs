import { existsSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const directory = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(directory, "..");
const bin = path.resolve(
  root,
  "../OpenCode/node_modules/.bin",
  process.platform === "win32" ? "tsgo.exe" : "tsgo",
);

if (!existsSync(bin)) {
  process.stderr.write(
    "Pinned OpenCode tsgo is missing; run bun install --frozen-lockfile in ../OpenCode.\n",
  );
  process.exit(1);
}

const result = spawnSync(bin, ["--noEmit", "-p", "tsconfig.typecheck.json"], {
  cwd: root,
  stdio: "inherit",
  windowsHide: true,
});
process.exit(result.status ?? 1);
