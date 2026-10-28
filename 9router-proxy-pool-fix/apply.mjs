#!/usr/bin/env node
/**
 * 9router proxy-pool fix: a health test must not disable the pool.
 *
 *   node apply.mjs            patch the installed 9router (auto-detected)
 *   node apply.mjs --check    report whether the fix is present, change nothing
 *   node apply.mjs --revert   undo the patch
 *   node apply.mjs --dir PATH patch a specific 9router install
 *
 * Works on the two shapes 9router ships in:
 *   - source checkout  -> src/app/api/proxy-pools/[id]/test/route.js
 *   - npm install      -> app/.next-cli-build/server/app/api/proxy-pools/[id]/test/route.js
 *
 * Why: POST /api/proxy-pools/[id]/test wrote `isActive: result.ok`, so one
 * failed test switched the pool off. Nothing re-tests automatically, so it
 * stayed off until a human toggled it — a single transient blip (a node
 * mid-rotation, a slow moment) silently removed a healthy pool from rotation.
 * The fix keeps isActive as the user's on/off switch and lets testStatus
 * report health. That is also what the UI already assumes: it shows
 * "Disable Dead Proxies" as a *prompt* after a bulk test, i.e. disabling is a
 * decision it asks the user to make, not something the test does by itself.
 *
 * Every edit is verified before it is written, and a .bak is left next to the
 * file, so a bad run cannot leave a broken build behind.
 */
import { readFileSync, writeFileSync, existsSync, copyFileSync, mkdtempSync, rmSync } from "node:fs";
import { join, dirname } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

const HERE = dirname(fileURLToPath(import.meta.url));

const SOURCE_REL = "src/app/api/proxy-pools/[id]/test/route.js";
const BUILD_REL = "app/.next-cli-build/server/app/api/proxy-pools/[id]/test/route.js";

// --- the two shapes of the same statement -----------------------------------
// source: multi-line, reads `isActive: result.ok,`
// Matched with a line-ending-tolerant pattern: a Windows checkout has CRLF, and
// a CRLF file is exactly what a git clone on Windows gives you by default.
// The lookahead keeps the line ending out of the match, so the replacement
// inherits the file's own EOL instead of inventing one.
const SRC_LINE = /^[ \t]*isActive: result\.ok,[ \t]*(?=\r?$)/m;
// build: minified, `...,isActive:g.ok}),`
const BUILD_NEEDLE = ",isActive:g.ok}";

const MARKER = "9router-proxy-pool-fix";

function findInstall(explicit) {
  if (explicit) return explicit;
  const candidates = [
    process.env.NINEROUTER_DIR,
    // npm global on Windows / Unix, and a local node_modules
    join(process.env.APPDATA || "", "npm/node_modules/9router"),
    "/usr/local/lib/node_modules/9router",
    "/usr/lib/node_modules/9router",
    join(process.env.HOME || "", ".npm-global/lib/node_modules/9router"),
    join(process.cwd(), "node_modules/9router"),
  ].filter(Boolean);
  for (const c of candidates) {
    if (existsSync(join(c, "package.json")) &&
        (existsSync(join(c, SOURCE_REL)) || existsSync(join(c, BUILD_REL)))) {
      return c;
    }
  }
  // last resort: ask npm where it lives (no shell: args go straight to npm)
  try {
    const root = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
    const c = join(root, "9router");
    if (existsSync(join(c, "package.json"))) return c;
  } catch { /* npm missing: fall through */ }
  return null;
}

/**
 * Parse a candidate result before it is allowed anywhere near disk.
 * Returns null when it is valid, or the parser's complaint.
 * Source is an ES module; the build is a CommonJS bundle.
 */
function checkSyntax(text, kind) {
  const ext = kind === "source" ? ".mjs" : ".cjs";
  let dir;
  try {
    dir = mkdtempSync(join(tmpdir(), "9r-check-"));
    const probe = join(dir, "probe" + ext);
    writeFileSync(probe, text);
    execFileSync(process.execPath, ["--check", probe], { stdio: "pipe" });
    return null;
  } catch (err) {
    const msg = (err.stderr || err.stdout || err.message || "").toString();
    const line = msg.split("\n").find((l) => l.includes("Error")) || msg.split("\n")[0];
    return line.trim() || "parse failed";
  } finally {
    if (dir) { try { rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ } }
  }
}

function targets(dir) {
  const out = [];
  for (const rel of [SOURCE_REL, BUILD_REL]) {
    const p = join(dir, rel);
    if (existsSync(p)) out.push({ path: p, rel, kind: rel === SOURCE_REL ? "source" : "build" });
  }
  return out;
}

function state(text, kind) {
  if (text.includes(MARKER)) return "patched";
  const found = kind === "source" ? SRC_LINE.test(text) : text.includes(BUILD_NEEDLE);
  return found ? "unpatched" : "unknown";
}

function applyOne({ path, rel, kind }) {
  const before = readFileSync(path, "utf8");
  const st = state(before, kind);
  if (st === "patched") return { rel, result: "already patched" };
  if (st === "unknown") {
    return { rel, result: "SKIPPED - neither the known statement nor the marker was found (different 9router version?)" };
  }

  let after;
  if (kind === "source") {
    // keep the source readable: comment in place of the line, same EOL as the file
    const eol = before.includes("\r\n") ? "\r\n" : "\n";
    after = before.replace(
      SRC_LINE,
      `      // ${MARKER}: a test reports health, it does not switch the pool off.${eol}` +
      `      // isActive stays the user's switch; testStatus carries the verdict.`
    );
  } else {
    after = before.replace(BUILD_NEEDLE, "}");
    if (after === before) return { rel, result: "SKIPPED - build replace produced no change" };
    // Stamp the build so --check and re-runs recognise it. Appended, never
    // spliced in: this file is minified and starts with a "use strict"
    // directive, so inserting a comment near the top is how you break it.
    after = after + `\n/*${MARKER}*/\n`;
  }

  // verify: the disabling write is gone, and the result still parses. The
  // parse check is the one that matters — a syntactically broken build route
  // takes the whole 9router API down, and "the string changed" says nothing
  // about that.
  const stillDisables = kind === "source"
    ? SRC_LINE.test(after)
    : after.includes(BUILD_NEEDLE);
  if (stillDisables) return { rel, result: "FAILED - statement still present after edit" };

  const parse = checkSyntax(after, kind);
  if (parse) return { rel, result: `FAILED - result does not parse: ${parse}` };

  copyFileSync(path, path + ".bak");
  writeFileSync(path, after);
  return { rel, result: "patched (backup: " + rel.split("/").pop() + ".bak)" };
}

function revertOne({ path, rel, kind }) {
  const bak = path + ".bak";
  if (!existsSync(bak)) return { rel, result: "no .bak - nothing to revert to" };
  const restored = readFileSync(bak, "utf8");
  const st = state(restored, kind);
  if (st !== "unpatched") return { rel, result: "refusing: the .bak is not the original file" };
  writeFileSync(path, restored);
  return { rel, result: "reverted" };
}

// --- main -------------------------------------------------------------------
const args = process.argv.slice(2);
const flag = (f) => args.includes(f);
const value = (f) => { const i = args.indexOf(f); return i >= 0 ? args[i + 1] : null; };

const dir = findInstall(value("--dir"));
if (!dir) {
  console.error("Could not find a 9router install.");
  console.error("Pass one explicitly:  node apply.mjs --dir /path/to/9router");
  process.exit(2);
}

const list = targets(dir);
if (list.length === 0) {
  console.error(`No proxy-pool test route under ${dir}`);
  process.exit(2);
}

console.log(`9router at ${dir}`);
let unpatched = 0, failed = 0;
for (const t of list) {
  const text = readFileSync(t.path, "utf8");
  const st = state(text, t.kind);
  if (flag("--check")) {
    console.log(`  ${t.kind.padEnd(7)} ${st}`);
    if (st !== "patched") unpatched++;
    continue;
  }
  const r = flag("--revert") ? revertOne(t) : applyOne(t);
  console.log(`  ${t.kind.padEnd(7)} ${r.result}`);
  if (r.result.startsWith("FAILED") || r.result.startsWith("SKIPPED")) failed++;
}

if (flag("--check")) {
  console.log(unpatched === 0 ? "\nAll copies are patched." : "\nNot patched.");
  process.exit(unpatched === 0 ? 0 : 1);
}

if (!flag("--revert") && failed === 0) {
  console.log("\nDone. Restart 9router so the new build is loaded:");
  console.log("  9router  (or: systemctl --user restart 9router)");
  console.log("\nAfter restarting, a failed health test shows as testStatus=error");
  console.log("but leaves the pool enabled. Toggle it off yourself if you want it out.");
}
process.exit(failed === 0 ? 0 : 1);
