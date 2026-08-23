/**
 * Bug-6517: verify that the Excel chatStrings i18n map covers the shared-ui
 * key contract and that parameterized entries interpolate the correct param
 * names WITHOUT re-formatting the incoming (already display-ready) values.
 *
 * This asserts STRUCTURALLY against the canonical
 * `frontend/src/i18n/en/shared-chat.json` rather than a hand-maintained key
 * list. A hand list silently drifts: it once misclassified the parameterized
 * `chart.ariaLabel` as a bare key, and let `turn.rawRecordsNotSupported` go
 * missing from the plugin entirely (deep-review findings). Deriving the
 * contract from the source file makes both classes impossible to reintroduce.
 */
import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import canonical from "../../../frontend/src/i18n/en/shared-chat.json";
import { chatT, CHAT_STRING_KEYS } from "../i18n/chatStrings";

const CANONICAL: Record<string, string> = canonical as Record<string, string>;

/** Extract `{{param}}` names from a canonical template. */
function paramNames(template: string): string[] {
  const names: string[] = [];
  const re = /\{\{(\w+)\}\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(template)) !== null) names.push(m[1]);
  return names;
}

// Bug-7397 follow-up (parity guard 3-way drift gap): the guards below only
// assert canonical keys ⊆ plugin keys. Neither direction was ever checked
// against shared-ui's actual `t()` call sites, so a shared-ui component could
// add a brand-new translation key without the canonical shared-chat.json OR
// this plugin ever being told -- a 3-way drift no existing guard caught. This
// derives the used-key set directly from shared-ui's component source (the
// real call sites the ChatCanvas tree renders through) and asserts it against
// the canonical contract, entirely within this suite (no dependency on the
// frontend suite also being run).
//
// KNOWN LIMITATION (deep-review finding, disclosed rather than silently
// assumed away): the regex below only sees a literal string passed directly
// to `t(...)`. It CANNOT see a key resolved indirectly through a lookup table
// -- e.g. JudgeVerdictStrip.tsx builds a `VERDICT_META` map of `{ key: "..."
// }` entries and calls `t(meta.key)`; the literal `judge.*` keys inside that
// table are invisible to this scan even though they are genuinely rendered.
// It also cannot see a key supplied at runtime by the backend (e.g.
// AssistantTurn.tsx resolves `guardrail_actions[].message_i18n_key` from the
// agent-service response and passes THAT string to `t()`). This guard closes
// the static-literal class of 3-way drift; it does not claim to close the
// lookup-table or backend-supplied-key classes -- those need either an
// extended AST-aware scan or a canonical/producer contract test of their own,
// tracked as a follow-up rather than silently claimed as covered here.
const here = dirname(fileURLToPath(import.meta.url));
const SHARED_UI_ROOT = join(here, "../../../shared-ui/src");

function walkSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const st = statSync(full);
    if (st.isDirectory()) out.push(...walkSourceFiles(full));
    else if (/\.(ts|tsx)$/.test(name)) out.push(full);
  }
  return out;
}

/**
 * Every key shared-ui's component source actually calls `t(key, ...)` with,
 * where `key` is a literal string at the call site. See the KNOWN LIMITATION
 * note above for what this intentionally does not cover.
 */
function collectSharedUiTKeys(): Set<string> {
  const keys = new Set<string>();
  const callRe = /\bt\(\s*["']([^"']+)["']/g;
  for (const file of walkSourceFiles(SHARED_UI_ROOT)) {
    const text = readFileSync(file, "utf8");
    let m: RegExpExecArray | null;
    while ((m = callRe.exec(text)) !== null) keys.add(m[1]);
  }
  return keys;
}

describe("chatStrings i18n key parity (Bug-6517)", () => {
  it("covers every canonical shared-chat key (no key falls back to the raw key)", () => {
    const missing = Object.keys(CANONICAL).filter((key) => !CHAT_STRING_KEYS.has(key));
    expect(missing).toEqual([]);
  });

  it("every shared-ui t('literal-key') call-site key exists in the canonical shared-chat.json (3-way drift guard)", () => {
    // Closes the STATIC-LITERAL gap the two direction-limited guards above
    // leave open: this is the ONLY check in either suite that looks at what
    // shared-ui actually calls. If a component adds `t("some.newKey")`
    // without also adding `some.newKey` to shared-chat.json, this fails here
    // -- before it can silently render as a raw key in both the web host and
    // the task pane. See the KNOWN LIMITATION comment above collectSharedUiTKeys
    // for the classes of key usage (lookup-table-resolved, backend-supplied)
    // this specific guard does not and cannot cover.
    const usedKeys = collectSharedUiTKeys();
    expect(usedKeys.size).toBeGreaterThan(0); // sanity: the scan found real call sites
    const missingFromCanonical = [...usedKeys].filter((key) => !(key in CANONICAL));
    expect(missingFromCanonical).toEqual([]);
  });

  it("every canonical key resolves to a translated string, not the raw key", () => {
    const unresolved: string[] = [];
    for (const key of Object.keys(CANONICAL)) {
      // Supply sentinel params for any placeholders so a parameterized key
      // does not resolve to a leftover template.
      const params: Record<string, string> = {};
      for (const name of paramNames(CANONICAL[key])) params[name] = `§${name}`;
      const result = chatT(key, Object.keys(params).length ? params : undefined);
      if (result === key) unresolved.push(key);
    }
    expect(unresolved).toEqual([]);
  });

  it("every key's template matches the canonical English value verbatim (except an intentional allow-list)", () => {
    // Keys the Excel task pane intentionally words differently from the web app
    // (e.g. a more descriptive composer prompt). Every other key -- INCLUDING
    // parameterized templates -- must match shared-chat.json verbatim so the two
    // hosts do not silently diverge (an aria-label mismatch is a real a11y
    // divergence; a reworded "{{count}} rows" template is a silent content drift
    // the param-name check below would not catch). chatT(key) with no params
    // returns the raw template (placeholders unsubstituted), so it equals the
    // canonical template for both plain and parameterized keys.
    const INTENTIONAL_DIVERGENCE = new Set<string>([
      "composer.placeholder", // Excel: "Ask a question about your data..."
    ]);
    const divergent: { key: string; plugin: string; canonical: string }[] = [];
    for (const key of Object.keys(CANONICAL)) {
      if (INTENTIONAL_DIVERGENCE.has(key)) continue;
      const plugin = chatT(key);
      if (plugin !== CANONICAL[key]) divergent.push({ key, plugin, canonical: CANONICAL[key] });
    }
    expect(divergent).toEqual([]);
  });

  it("parameterized keys interpolate the producer's param names with no re-coercion", () => {
    // The producers pass display-ready values (e.g. count: "1,234"). A sentinel
    // that is NOT a bare number (`§count`) proves two things at once: the
    // param NAME matches (the sentinel survives into the output, no leftover
    // `{{...}}`), and the value is NOT re-coerced through Number() (which would
    // turn "1,234"/"§count" into "NaN" — the exact task-pane defect).
    const offenders: { key: string; result: string }[] = [];
    for (const key of Object.keys(CANONICAL)) {
      const names = paramNames(CANONICAL[key]);
      if (names.length === 0) continue;
      const params: Record<string, string> = {};
      for (const name of names) params[name] = `§${name}`;
      const result = chatT(key, params);
      const ok =
        !result.includes("{{") &&
        names.every((name) => result.includes(`§${name}`));
      if (!ok) offenders.push({ key, result });
    }
    expect(offenders).toEqual([]);
  });

  // Human-readable spot checks (documentation value); the generic guards above
  // enforce the whole contract.
  it("badges.rows renders a pre-grouped count verbatim (no NaN)", () => {
    const result = chatT("badges.rows", { count: (1234).toLocaleString() });
    expect(result).toBe("1,234 rows");
    expect(result).not.toContain("NaN");
  });

  it("turn.rawRecordsNotSupported resolves to the canonical refusal detail", () => {
    const result = chatT("turn.rawRecordsNotSupported");
    expect(result).toBe(CANONICAL["turn.rawRecordsNotSupported"]);
    expect(result).not.toBe("turn.rawRecordsNotSupported");
  });

  it("chart.ariaLabel uses param name 'title' (shared ChartBlock passes { title })", () => {
    const result = chatT("chart.ariaLabel", { title: "Sales" });
    expect(result).toBe("Sales chart. Expand the data table below for the underlying values.");
    expect(result).not.toContain("undefined");
  });

  it("steps.header / steps.step use param name 'n' (shared InlineStepCard passes { n })", () => {
    expect(chatT("steps.header", { n: "3" })).toBe("Steps (3)");
    expect(chatT("steps.step", { n: "2" })).toBe("Step 2");
  });

  it("chart.truncated uses param names 'count' and 'total' (shared ChartBlock passes { count, total })", () => {
    const result = chatT("chart.truncated", { count: 50, total: 200 });
    expect(result).toBe("Showing first 50 of 200 rows");
    expect(result).not.toContain("NaN");
  });

  it("trace.routeWithValue / trace.toolWithValue interpolate their values", () => {
    expect(chatT("trace.routeWithValue", { route: "aggregate" })).toBe("Route: aggregate");
    expect(chatT("trace.toolWithValue", { tool: "calculator" })).toBe("Tool: calculator");
  });
});
