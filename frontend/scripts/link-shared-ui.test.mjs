// Bug-8815 / Bug-9570 / R2-08 (TP-03): guards for link-shared-ui.js.
//
// This runs under Node's built-in test runner, not vitest — vitest's project
// config (vitest.config.ts) is jsdom-environment globally with a setupFiles
// entry that assumes jsdom globals (HTMLCanvasElement, etc.), which conflicts
// with a plain filesystem/Node-environment test. Node's test runner needs no
// config changes and no environment override.
//
// Run with: node --test scripts/link-shared-ui.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, mkdirSync, writeFileSync, readFileSync, symlinkSync, lstatSync, readlinkSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

const REAL_SCRIPT_PATH = fileURLToPath(new URL("./link-shared-ui.js", import.meta.url));
const SCRIPT_SOURCE = readFileSync(REAL_SCRIPT_PATH, "utf8");

// Build a throwaway <parent>/frontend/scripts + <parent>/shared-ui layout so
// the script's own path derivation (frontendDir = two levels up from the
// script; parentDir = one level up from that) resolves correctly, without
// touching the real repo tree. The script's derivation is based on its own
// `import.meta.url`, so a COPY of the real source must be placed at the
// fixture's script path (a stub/empty file would resolve paths against the
// real repo location instead of the fixture).
function makeFixture() {
  const root = mkdtempSync(join(tmpdir(), "link-shared-ui-test-"));
  const frontendDir = join(root, "frontend");
  const scriptsDir = join(frontendDir, "scripts");
  mkdirSync(scriptsDir, { recursive: true });
  mkdirSync(join(frontendDir, "node_modules"), { recursive: true });
  mkdirSync(join(root, "shared-ui"), { recursive: true });
  writeFileSync(join(scriptsDir, "link-shared-ui.js"), SCRIPT_SOURCE);
  return { root, frontendDir, scriptsDir };
}

function runScript(scriptsDir) {
  execFileSync(process.execPath, [join(scriptsDir, "link-shared-ui.js")], {
    cwd: scriptsDir,
    env: { ...process.env },
  });
}

test("repoints a dangling symlink instead of throwing EEXIST (Bug-9570)", () => {
  const { root, frontendDir, scriptsDir } = makeFixture();
  try {
    const linkPath = join(root, "node_modules");
    symlinkSync("/nonexistent-target-path-for-test", linkPath);
    assert.equal(existsSync(linkPath), false, "sanity: existsSync must report the dangling link as absent");

    runScript(scriptsDir);

    const stat = lstatSync(linkPath);
    assert.ok(stat.isSymbolicLink(), "expected a symlink at the parent node_modules path");
    assert.equal(readlinkSync(linkPath), join(frontendDir, "node_modules"));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("is idempotent on a second run once the link is already correct", () => {
  const { root, frontendDir, scriptsDir } = makeFixture();
  try {
    runScript(scriptsDir);
    const linkPath = join(root, "node_modules");
    const targetAfterFirstRun = readlinkSync(linkPath);

    runScript(scriptsDir); // second run must not throw and must leave the link unchanged

    assert.equal(readlinkSync(linkPath), targetAfterFirstRun);
    assert.equal(targetAfterFirstRun, join(frontendDir, "node_modules"));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("leaves a real directory at the link path untouched", () => {
  const { root, scriptsDir } = makeFixture();
  try {
    const linkPath = join(root, "node_modules");
    mkdirSync(linkPath);
    writeFileSync(join(linkPath, "sentinel"), "do-not-touch");

    runScript(scriptsDir);

    const stat = lstatSync(linkPath);
    assert.equal(stat.isSymbolicLink(), false, "a real directory must not be replaced with a symlink");
    assert.ok(existsSync(join(linkPath, "sentinel")), "the real directory's contents must survive");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("no-ops when shared-ui is not present yet (Docker's staged COPY order)", () => {
  const { root, scriptsDir } = makeFixture();
  try {
    rmSync(join(root, "shared-ui"), { recursive: true, force: true });
    const linkPath = join(root, "node_modules");

    runScript(scriptsDir); // must exit 0 and create nothing

    assert.equal(existsSync(linkPath), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
