#!/usr/bin/env node
// Bug-8815: shared-ui is a source-only sibling package with no node_modules
// of its own (see ../shared-ui/README.md). tsc follows the frontend's
// @tessallite/shared-ui/* path alias into shared-ui/src and needs to resolve
// its third-party imports (react, MUI, echarts, ...) by walking up from
// shared-ui/ — exactly what an npm-workspace hoist would produce. Docker and
// CI already create this symlink manually; this script automates the same
// step for a plain local `npm install` in frontend/, so a fresh checkout does
// not require the undocumented extra step of reading shared-ui/README.md.
import { existsSync, readlinkSync, symlinkSync, unlinkSync } from "node:fs";
import { lstat } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = dirname(dirname(fileURLToPath(import.meta.url)));
const parentDir = dirname(frontendDir);
const sharedUiDir = join(parentDir, "shared-ui");
const linkPath = join(parentDir, "node_modules");
const linkTarget = join(frontendDir, "node_modules");

if (!existsSync(sharedUiDir)) {
  process.exit(0);
}

// Bug-9570 (DR-07): probe with lstat, not existsSync — existsSync follows a
// symlink to its target, so a DANGLING symlink at ../node_modules (its old
// target since deleted, e.g. a sibling's node_modules that was removed)
// reports "does not exist" and falls through to symlinkSync below, which
// then throws EEXIST on the stale link and aborts npm install/ci entirely.
let existingStat = null;
try {
  existingStat = await lstat(linkPath);
} catch {
  existingStat = null; // genuinely absent
}

if (existingStat) {
  if (!existingStat.isSymbolicLink()) {
    // A real directory already sits at ../node_modules — leave it alone;
    // this is not this script's file to touch.
    process.exit(0);
  }
  let currentTarget = null;
  try {
    currentTarget = readlinkSync(linkPath);
  } catch {
    currentTarget = null; // dangling — readlink still succeeds on Linux/macOS,
    // this catch is for platforms where it does not
  }
  if (currentTarget === linkTarget) {
    process.exit(0);
  }
  // Bug-9570 (DR-07): the existing symlink points elsewhere — either
  // dangling, or a sibling consumer's own manual `ln -sfn` (excel-plugin,
  // conversational-client per shared-ui/README.md). Warn before repointing
  // it, since that sibling's tsc resolution will silently start failing.
  console.warn(
    `[link-shared-ui] Replacing existing ../node_modules link ` +
      `(previously -> ${currentTarget ?? "<dangling>"}) with -> ${linkTarget}. ` +
      `If another consumer (excel-plugin, conversational-client) relies on ` +
      `it pointing elsewhere, re-run its own setup step after installing here.`,
  );
  unlinkSync(linkPath);
}

symlinkSync(linkTarget, linkPath, "junction");
