import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import sharedChatMessages from "./en/shared-chat.json";

const here = dirname(fileURLToPath(import.meta.url));
const sharedUiRoot = join(here, "../../../shared-ui/src");
const source = sharedChatMessages as Record<string, string>;

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const st = statSync(full);
    if (st.isDirectory()) {
      out.push(...walk(full));
    } else if (/\.(ts|tsx)$/.test(name)) {
      out.push(full);
    }
  }
  return out;
}

function placeholders(value: string): string[] {
  return [...value.matchAll(/\{\{(\w+)\}\}/g)].map((m) => m[1]).sort();
}

/**
 * Bug-6757: extract the balanced-brace parameter block from a t() call.
 * The previous regex `\{([\s\S]*?)\}` matched only to the first `}`,
 * breaking on nested objects (e.g. `{ count, fn: helper({ x }) }`).
 * This function walks braces to find the correct closing `}`.
 */
function extractBalancedBraces(text: string, openIdx: number): string {
  let depth = 0;
  for (let i = openIdx; i < text.length; i++) {
    if (text[i] === "{") depth++;
    else if (text[i] === "}") {
      depth--;
      if (depth === 0) return text.slice(openIdx + 1, i);
    }
  }
  // Unbalanced — return whatever is between the opening brace and end.
  return text.slice(openIdx + 1);
}

/**
 * Bug-6757: collect ONLY the top-level parameter names from a t() param block.
 * A plain `\b(\w+)\s*:` scan over the whole block also matches keys of nested
 * object literals (e.g. `{ max: String(n), meta: { unit: "x" } }` would leak
 * `unit`), inflating the expected-param set and producing false drift. Walk the
 * brace depth and only record identifiers that appear at depth 1, and skip the
 * contents of string/template literals so a `:` inside a string is ignored.
 *
 * An identifier is only a KEY when it sits in property-key position — at the
 * block start or immediately after a top-level `,` — and is followed by `:`.
 * This rejects value-position identifiers that happen to precede a `:`, e.g.
 * the true-branch of a ternary value `{ label: cond ? a : b }` (here `a` is a
 * value, not a key), which a "followed by `:`" check alone would misread.
 */
function topLevelParamNames(block: string): string[] {
  const names: string[] = [];
  let depth = 1; // `block` is already inside the outer { }
  let quote: string | null = null;
  let keyPosition = true; // start of the block is a key position
  const identRe = /[A-Za-z_]\w*/y;
  for (let i = 0; i < block.length; i++) {
    const ch = block[i];
    if (quote) {
      if (ch === "\\") i++; // skip escaped char
      else if (ch === quote) quote = null;
      continue;
    }
    if (/\s/.test(ch)) continue; // whitespace does not change key position
    if (ch === '"' || ch === "'" || ch === "`") {
      quote = ch;
      keyPosition = false;
      continue;
    }
    if (ch === "{" || ch === "(" || ch === "[") {
      depth++;
      keyPosition = false;
      continue;
    }
    if (ch === "}" || ch === ")" || ch === "]") {
      depth--;
      keyPosition = false;
      continue;
    }
    if (depth === 1 && ch === ",") {
      keyPosition = true; // next top-level token is a fresh key
      continue;
    }
    if (depth === 1 && keyPosition && /[A-Za-z_]/.test(ch)) {
      identRe.lastIndex = i;
      const m = identRe.exec(block);
      if (m) {
        const rest = block.slice(identRe.lastIndex);
        // A key is an identifier in key position followed by `:`.
        if (/^\s*:/.test(rest)) names.push(m[0]);
        i = identRe.lastIndex - 1;
        keyPosition = false;
      }
      continue;
    }
    if (depth === 1) {
      // Any other top-level token (`:`, operators, value identifiers) leaves
      // key position until the next top-level comma.
      keyPosition = false;
    }
  }
  return names;
}

function collectSharedUiTCalls(): Map<string, Set<string>> {
  const calls = new Map<string, Set<string>>();
  // Match `t("key"` or `t('key'` — capture the key but stop before the
  // optional parameter object. We handle the parameter block separately
  // with the balanced-brace extractor so nested braces are handled.
  const callRe = /\bt\(\s*["']([^"']+)["']\s*/g;
  for (const file of walk(sharedUiRoot)) {
    const text = readFileSync(file, "utf8");
    let call: RegExpExecArray | null;
    while ((call = callRe.exec(text)) !== null) {
      const key = call[1];
      const params = calls.get(key) ?? new Set<string>();
      // After the key+quote, check if the next non-whitespace is `,` then `{`.
      const afterKey = text.slice(callRe.lastIndex);
      const commaMatch = afterKey.match(/^,\s*\{/);
      if (commaMatch) {
        const braceStart = callRe.lastIndex + (commaMatch.index ?? 0) + commaMatch[0].length - 1;
        const paramBlock = extractBalancedBraces(text, braceStart);
        for (const name of topLevelParamNames(paramBlock)) {
          params.add(name);
        }
      }
      calls.set(key, params);
    }
  }
  return calls;
}

describe("shared-ui shared-chat i18n producer contract (Bug-6517/Bug-6526)", () => {
  it("main SPA shared-chat.json contains every static shared-ui t() key", () => {
    const calls = collectSharedUiTCalls();
    const missing = [...calls.keys()].filter((key) => !(key in source));

    expect(missing).toEqual([]);
  });

  it("shared-chat placeholders match the parameter names passed by shared-ui", () => {
    const calls = collectSharedUiTCalls();
    const drift: string[] = [];
    for (const [key, params] of calls) {
      const expected = [...params].sort();
      const actual = placeholders(source[key] ?? "");
      if (expected.join("\0") !== actual.join("\0")) {
        drift.push(`${key}: shared-ui={${expected.join(",")}} source={${actual.join(",")}}`);
      }
    }

    expect(drift, `shared-ui t() params differ from shared-chat placeholders:\n${drift.join("\n")}`).toEqual([]);
  });
});
