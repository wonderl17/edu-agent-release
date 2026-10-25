"use strict";

// Trusted controller preload. Never serialize commands, arguments, environment,
// child output, exceptions, paths or numeric exit codes into this channel.
// Only bounded elapsed milliseconds and fixed platform labels may be added.
const fs = require("node:fs");
const path = require("node:path");
const childProcess = require("node:child_process");
const { syncBuiltinESMExports } = require("node:module");
const { errorMonitor } = require("node:events");

const file = process.env.RELEASE_DESKTOP_CHECKPOINT_FILE;
const maxBytes = 16384;
const stages = new Set([
  "D_ENTRY", "D_TOOLCHAIN", "D_DEPENDENCIES", "D_CHANGELOG", "D_NODE_CHECK",
  "D_VPK_SETUP", "D_WINDOWS_SETUP", "D_MAC_SIGN", "D_PACKAGE", "D_PREFLIGHT",
  "D_WEB_BUILD", "D_VITE", "D_SIDECAR", "D_ELECTRON", "D_LATEX", "D_PDF",
  "D_VPK_DOWNLOAD", "D_VPK_PACK", "D_VPK_DELTA", "D_VERIFY", "D_R2_PUBLISH",
  "D_FINAL_PUBLISH", "D_RESUME", "D_TOKEN_REFRESH", "D_CACHE_RESTORE", "D_CACHE_SAVE",
]);
const states = new Set(["start", "success", "failure"]);

function record(stage, state, started = performance.now()) {
  if (!file || !stages.has(stage) || !states.has(state)) return;
  let descriptor;
  let locked = false;
  try {
    // A nonblocking lock bounds concurrent writers too; diagnostic contention
    // may drop a record, but must never delay a build or change its result.
    fs.mkdirSync(`${file}.lock`, { mode: 0o700 });
    locked = true;
    descriptor = fs.openSync(file, fs.constants.O_RDWR | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
    const metadata = fs.fstatSync(descriptor);
    const scope = ["mac-arm64", "mac-x64", "win-x64"].includes(process.env.DESKTOP_PACK_TARGET) ? process.env.DESKTOP_PACK_TARGET : "all";
    const elapsed = state === "start" ? 0 : Math.min(10800000, Math.max(0, Math.round(performance.now() - started)));
    const line = `${stage} ${state} ${scope} ${elapsed}\n`;
    if (metadata.isFile() && (metadata.mode & 0o777) === 0o600
        && metadata.uid === process.getuid()) {
      const next = Buffer.from(line);
      if (metadata.size + next.length <= maxBytes) {
        fs.writeSync(descriptor, next, 0, next.length, metadata.size);
      } else {
        // Retain complete recent records, including the final failing child.
        const tail = Buffer.alloc(Math.min(metadata.size, maxBytes - next.length));
        fs.readSync(descriptor, tail, 0, tail.length, metadata.size - tail.length);
        const firstComplete = metadata.size > tail.length ? tail.indexOf(10) + 1 : 0;
        const recent = Buffer.concat([tail.subarray(firstComplete), next]);
        fs.writeSync(descriptor, recent, 0, recent.length, 0);
        fs.ftruncateSync(descriptor, recent.length);
      }
    }
  } catch {
    // No error details or source-controlled strings may escape the recorder.
  } finally {
    if (descriptor !== undefined) { try { fs.closeSync(descriptor); } catch {} }
    if (locked) { try { fs.rmdirSync(`${file}.lock`); } catch {} }
  }
}

function nodeStage(entry) {
  if (typeof entry !== "string") return null;
  switch (path.basename(entry)) {
    case "ci-release-desktop.mjs": return "D_ENTRY";
    case "pack-desktop-release.mjs": return process.argv.includes("--prepared") ? "D_PACKAGE" : null;
    case "release-preflight.mjs": return "D_PREFLIGHT";
    case "vite.js": case "vite": return "D_VITE";
    case "stage-sidecar-runtime.cjs": return "D_SIDECAR";
    case "stage-tectonic-runtime.cjs": return "D_LATEX";
    case "stage-pdf-runtime.cjs": return "D_PDF";
    case "cli.js":
      return entry.replaceAll("\\", "/").includes("/electron-builder/") ? "D_ELECTRON" : null;
    default: return null;
  }
}

function commandStage(command, args) {
  if (typeof command !== "string" || !Array.isArray(args)) return null;
  const name = path.basename(command).replace(/\.cmd$/, "");
  if (name === "npm" && args[0] === "install" && args.includes("--global")) return "D_TOOLCHAIN";
  if (name === "pnpm") {
    if (args[0] === "install") return "D_DEPENDENCIES";
    if (args[0] === "changelog:check") return "D_CHANGELOG";
    if (args[0] === "pack:desktop:all") return "D_PACKAGE";
    if (args[0] === "release:desktop:preflight") return "D_PREFLIGHT";
    if (args[0] === "release:desktop:r2:existing") return "D_R2_PUBLISH";
    if (args[0] === "release:desktop:all:existing") return "D_FINAL_PUBLISH";
    if (args[0] === "--filter" && args[1] === "@teaching-ai-agent/web" && args[2] === "build:electron") return "D_WEB_BUILD";
    if (args[0] === "--filter" && args[1] === "@teaching-ai-agent/desktop" && args[2] === "build:sidecar") return "D_SIDECAR";
  }
  if (name === "node") {
    if (path.basename(String(args[0])) === "desktop-build-cache.mjs") return args[1] === "restore" ? "D_CACHE_RESTORE" : "D_CACHE_SAVE";
    if (args[0] === "--input-type=module" && args[1] === "--eval") return "D_NODE_CHECK";
    if (path.basename(String(args[0])) === "resume-cos-release.mjs") return "D_RESUME";
  }
  if (name === "dotnet" && args[0] === "tool" && args[1] === "install") return "D_VPK_SETUP";
  if (name === "security" || name === "codesign") return "D_MAC_SIGN";
  if (name === "vpk") {
    let index = ["[osx]", "[win]"].includes(args[0]) ? 1 : 0;
    while (["--yes", "--skip-updates"].includes(args[index])) index++;
    if (args[index] === "download") return "D_VPK_DOWNLOAD";
    if (args[index] === "pack") return "D_VPK_PACK";
    if (args[index] === "delta") return "D_VPK_DELTA";
  }
  if (name === "unzip" || name === "ditto") return "D_VERIFY";
  if (name === "java" && (args[0] === "-version" || (args[0] === "-jar" && path.basename(String(args[1])) === "jsign.jar"))) return "D_WINDOWS_SETUP";
  if (name === "curl" && args.includes("https://github.com/ebourg/jsign/releases/download/7.4/jsign-7.4.jar")) return "D_WINDOWS_SETUP";
  if (name === "az" && args[0] === "account" && args[1] === "get-access-token") return "D_WINDOWS_SETUP";
  if (name === "python3" && args[1] === "--token-for") return "D_TOKEN_REFRESH";
  return null;
}

function inheritedOptions(command, options) {
  // Reapply only the trusted preload even when a nested caller supplies a new
  // env object. All of that caller's NODE_OPTIONS are deliberately discarded.
  const env = { ...(options?.env || process.env) };
  delete env.NODE_OPTIONS;
  delete env.RELEASE_DESKTOP_CHECKPOINT_FILE;
  const name = typeof command === "string" ? path.basename(command).replace(/\.cmd$/, "") : "";
  const nodeOrShell = ["node", "npm", "pnpm", "npx", "bash", "sh", "zsh"].includes(name)
    || (typeof command === "string" && options?.shell && /^(?:node|npm|pnpm|npx)\s/.test(command));
  if (nodeOrShell) {
    env.NODE_OPTIONS = `--require=${JSON.stringify(__filename)}`;
    env.RELEASE_DESKTOP_CHECKPOINT_FILE = file;
  }
  return { ...options, env };
}

if (file) for (const method of ["spawn", "spawnSync"]) {
  const original = childProcess[method];
  childProcess[method] = function (command, args, options) {
    // Leave invalid overloads to Node's original validation and exception type.
    const optionsOnly = args != null && !Array.isArray(args);
    const sourceOptions = optionsOnly ? args : options;
    if ((optionsOnly && typeof args !== "object")
        || Array.isArray(sourceOptions)
        || (sourceOptions != null && typeof sourceOptions !== "object")) {
      return original.apply(this, arguments);
    }
    const values = Array.isArray(args) ? args : [];
    const stage = commandStage(command, values);
    const actualOptions = inheritedOptions(command, sourceOptions);
    const started = performance.now();
    record(stage, "start", started);
    try {
      const child = original.call(this, command, values, actualOptions);
      if (method === "spawnSync") {
        record(stage, child.status === 0 && !child.error ? "success" : "failure", started);
      } else {
        let finished = false;
        const finish = (state) => { if (!finished) { finished = true; record(stage, state, started); } };
        // errorMonitor preserves unhandled-error behavior of the original API.
        child.once(errorMonitor, () => finish("failure"));
        child.once("close", (code) => finish(code === 0 ? "success" : "failure"));
      }
      return child;
    } catch (error) {
      record(stage, "failure", started);
      throw error;
    }
  };
}
syncBuiltinESMExports();

const ownStage = nodeStage(process.argv[1]);
const ownStarted = performance.now();
record(ownStage, "start", ownStarted);
process.once("exit", (code) => record(ownStage, code === 0 ? "success" : "failure", ownStarted));
