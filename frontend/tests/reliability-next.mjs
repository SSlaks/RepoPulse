import { spawn } from "node:child_process";
import { rmSync } from "node:fs";
import { resolve, sep } from "node:path";

const nextRoot = resolve(".next");
const fetchCache = resolve(nextRoot, "cache", "fetch-cache");
if (!fetchCache.startsWith(`${nextRoot}${sep}`)) throw new Error("Refusing to clear a cache outside .next");
rmSync(fetchCache, { recursive: true, force: true });

const nextCli = resolve("node_modules", "next", "dist", "bin", "next");
const child = spawn(process.execPath, [nextCli, "start", "--hostname", "127.0.0.1", "--port", "13001"], {
  env: process.env,
  stdio: "inherit",
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => child.kill(signal));
}
child.on("exit", (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
