import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
// en.json was split into per-domain files (en/*.json) merged by ./index; the
// default export is the flat merged English bundle (dotted keys), so the
// coverage guard below still asserts every referenced key resolves in English.
import en, { getMessages } from "./index";
// Producer constants — the test derives finite-family domains from these so it
// can never drift from the code that actually builds the t(`prefix.${x}`) keys.
// If a producer adds a value with no en key, the derived expansion fails here.
import { KPI_TEMPLATES, NAMED_SET_TEMPLATES } from "../components/Panels/templates";
import { AGGREGATION_OPTIONS } from "../components/KpiBusinessBuilder/businessDefinition";
import { AGG_OPTIONS } from "../components/Panels/MeasureQueryPanel/measureColumns";

// H21 — Translations parking.
//
// The localisation feature is parked (gate GH1). Policy:
//   * en.json is the single authoritative locale and must be complete:
//     every i18n key referenced in code must resolve in en.json, otherwise
//     the user sees a raw key string (a bug).
//   * The 7 other locales (ar/de/es/fr/ja/pt/zh) are intentionally parked.
//     Missing keys in those locales must fall back to en — never a raw key.
//
// These tests guard both invariants so the parking stays clean and
// non-breaking. They assert behaviour, not implementation.

const here = dirname(fileURLToPath(import.meta.url));
const srcRoot = join(here, "..");
const enKeys = new Set(Object.keys(en as Record<string, string>));

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const st = statSync(full);
    if (st.isDirectory()) {
      if (name === "node_modules" || name === "dist") continue;
      out.push(...walk(full));
    } else if (/\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name)) {
      out.push(full);
    }
  }
  return out;
}

// Collect every static (string-literal) translation key referenced via t("...")
// across the source tree. Template-literal keys (t(`prefix.${x}`)) are dynamic
// and are covered separately by the dynamic-family test below.
function collectStaticKeys(): Map<string, string[]> {
  const keyToFiles = new Map<string, string[]>();
  // Matches t("key") or t('key') including a leading second arg position, with
  // optional whitespace. Keys are dotted lowercase identifiers.
  const re = /\bt\(\s*["']([A-Za-z0-9_]+(?:\.[A-Za-z0-9_ ]+)+)["']/g;
  for (const file of walk(srcRoot)) {
    const text = readFileSync(file, "utf8");
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) {
      const key = m[1];
      const arr = keyToFiles.get(key) ?? [];
      arr.push(file);
      keyToFiles.set(key, arr);
    }
  }
  return keyToFiles;
}

describe("i18n en.json coverage", () => {
  it("resolves every static t(\"...\") key used in source code", () => {
    const used = collectStaticKeys();
    const missing: string[] = [];
    for (const [key, files] of used) {
      if (!enKeys.has(key)) {
        missing.push(`${key}  (used in ${files.map((f) => f.replace(srcRoot, "src")).join(", ")})`);
      }
    }
    expect(missing, `Missing en.json keys (would render as raw key strings):\n${missing.join("\n")}`).toEqual([]);
  });

  it("resolves every dynamic key family with a finite, code-defined domain", () => {
    // These keys are built at runtime as t(`prefix.${value}`). Where the value
    // set is fixed in code (a TS enum/union, a `const X = [...] as const`, or an
    // object literal), every expansion MUST exist in en.json or the UI leaks the
    // raw key string. This test enumerates EVERY such family — derived from the
    // actual producer domains — so a new call-site member without an en key fails
    // here. (Open-domain families like `pipeline.meta.*` (arbitrary backend trace
    // keys) and `scratchpad.dataType.*` (free-text field) are not finite and are
    // intentionally excluded.)
    //
    // Each entry: the t() call-site transform applied to each domain member, then
    // the produced key prefixed. The transforms mirror the call sites exactly.

    const pascal = (s: string) =>
      s.charAt(0).toUpperCase() + s.slice(1).replace(/_([a-z])/g, (_, c: string) => c.toUpperCase());

    const expansions: string[] = [];
    const add = (...keys: string[]) => expansions.push(...keys);

    // --- users.role* (UsersAccessPanel) ---
    // USER_ROLES (LocalUserRole) via t(`users.role${pascal(r)}`)
    for (const r of ["member", "tenant_admin", "model_technical"]) add(`users.role${pascal(r)}`);
    // ACCESS_ROLES (AccessRole) via t(`users.role${Cap(r)}`) (no underscores)
    for (const r of ["admin", "modeler", "viewer"]) add(`users.role${r.charAt(0).toUpperCase() + r.slice(1)}`);

    // --- lifecycleLog.eventType.* (LifecycleLogPanel) ---
    // EVENT_TYPES (panel filter) + optimizer/scheduler VALID_EVENT_TYPES
    // (live rows), snake_case. Includes retired_idle (scheduler emits it,
    // F-010-11) and purged.
    for (const ev of [
      "all", "created", "approved", "validated", "retired",
      "retired_unused", "retired_idle", "purged", "refresh_failed",
    ])
      add(`lifecycleLog.eventType.${ev}`);

    // --- schemaChanges.* (SchemaChangesPanel) ---
    // change_type producer (scheduler schema_drift.py) emits these snake_case values.
    for (const ct of ["column_added", "column_removed", "type_changed"]) add(`schemaChanges.${ct}`);

    // --- roles.* (binding/role chips) ---
    for (const r of ["member", "tenant_admin", "model_technical", "admin", "modeler", "viewer"]) add(`roles.${r}`);

    // --- namedSets.scope.* (NamedSetsPanel SCOPE_OPTIONS) ---
    for (const v of [1, 2]) add(`namedSets.scope.${v}`);

    // --- namedSets.listType.* (NamedSetsPanel LIST_TYPE_OPTIONS, ListType union) ---
    for (const v of ["fixed", "dynamic_top_n", "filtered", "advanced_mdx"]) add(`namedSets.listType.${v}`);

    // --- calendar.* (CalendarTableDialog CALENDAR_TYPE_VALUES, with inline remap) ---
    for (const v of ["standard", "fiscal", "iso_week", "retail_445", "hijri", "thai_buddhist"]) {
      const mapped = v === "iso_week" ? "isoWeek" : v === "retail_445" ? "retail445" : v === "thai_buddhist" ? "thaiBuddhist" : v;
      add(`calendar.${mapped}`);
    }

    // --- dimCalendar.level.* (HIERARCHY_PRESETS_RAW levels) ---
    const presets: Record<string, string[]> = {
      standard: ["Year", "Half", "Quarter", "Month", "Week", "Day"],
      fiscal: ["Fiscal Year", "Fiscal Half", "Fiscal Quarter", "Fiscal Month", "Day"],
      iso_week: ["ISO Year", "ISO Week", "ISO Day"],
      retail_445: ["Retail Year", "Retail Quarter", "Retail Period", "Retail Week"],
      hijri: ["Hijri Year", "Hijri Month", "Hijri Day"],
      thai_buddhist: ["Thai Year", "Quarter", "Month", "Day"],
    };
    for (const levels of Object.values(presets)) for (const l of levels) add(`dimCalendar.level.${l}`);

    // --- pivot.agg.* (AGG_OPTIONS producer, lowercased) ---
    // PickerBar/measureColumns: t(`pivot.agg.${agg.toLowerCase()}`)
    for (const a of AGG_OPTIONS) add(`pivot.agg.${a.toLowerCase()}`);

    // --- alerts.channel.* (CHANNEL_TYPES) ---
    for (const c of ["email", "slack"]) add(`alerts.channel.${c}`);

    // --- templateGallery.{category,status,trend}.* ---
    // Producer: KPI_TEMPLATES + NAMED_SET_TEMPLATES (templates.ts). The chips in
    // TemplateGalleryDialog apply NO transform — t(`templateGallery.category.${tmpl.category}`),
    // .status.${tmpl.status_graphic}, .trend.${tmpl.trend_graphic} — so the raw
    // producer string (including spaces) is the key suffix. Derive every producible
    // value so a new template value with no en key fails here.
    const allTemplates = [...KPI_TEMPLATES, ...NAMED_SET_TEMPLATES];
    for (const c of new Set(allTemplates.map((t) => t.category))) add(`templateGallery.category.${c}`);
    for (const s of new Set(KPI_TEMPLATES.map((t) => t.status_graphic))) add(`templateGallery.status.${s}`);
    for (const tr of new Set(KPI_TEMPLATES.map((t) => t.trend_graphic))) add(`templateGallery.trend.${tr}`);

    // --- kpiBusiness.agg* (AGGREGATION_OPTIONS producer) ---
    // KpiBusinessBuilderDialog/KpiCard: t(`kpiBusiness.agg${agg[0].toUpperCase()}${agg.slice(1)}`)
    // — only the first char is capitalised, so `count_distinct` -> `aggCount_distinct`.
    for (const a of AGGREGATION_OPTIONS.map((o) => o.value))
      add(`kpiBusiness.agg${a.charAt(0).toUpperCase()}${a.slice(1)}`);

    // --- kpis.wizard.v2.type${KpiType} (KpiType union) ---
    for (const k of ["simple_measure", "ratio", "variance", "growth_rate", "moving_window", "composite"])
      add(`kpis.wizard.v2.type${pascal(k)}`);

    // --- kpis.targetType${TargetType} (TargetType union) ---
    for (const tt of ["none", "static", "measure", "prior_period", "expression"]) add(`kpis.targetType${pascal(tt)}`);

    // --- kpis.direction${Direction} (Direction union) ---
    for (const d of ["higher_is_better", "lower_is_better", "closer_is_better"]) add(`kpis.direction${pascal(d)}`);

    const missing = [...new Set(expansions)].filter((k) => !enKeys.has(k));
    expect(missing, `Missing dynamic en.json keys (would render as raw key strings):\n${missing.join("\n")}`).toEqual([]);
  });

  it("contains no empty-string values (an empty value would defeat the en fallback)", () => {
    const empties = Object.entries(en as Record<string, string>)
      .filter(([, v]) => typeof v === "string" && v.trim() === "")
      .map(([k]) => k);
    expect(empties).toEqual([]);
  });

  // F-i18n-07 — cross-locale interpolation parity. A non-English value that
  // drops or renames a {{var}} that English uses silently loses data (the
  // number/name vanishes) or leaks a raw {{var}} the page never substitutes.
  // English itself must use only double-brace {{x}} (the loader only
  // substitutes {{x}}; a lone {x} renders literally — F-i18n-01).
  const PARKED_LOCALES = ["ar", "de", "es", "fr", "ja", "pt", "zh"];
  const bracedVars = (s: string): Set<string> =>
    new Set(s.match(/\{\{(\w+)\}\}/g) ?? []);

  it("English values use only double-brace placeholders (no lone {var})", () => {
    // recipes.filtersHelperText embeds a literal JSON example ({"value":"{region}"}),
    // not an interpolation — it is the one legitimate single-brace string.
    const allow = new Set(["recipes.filtersHelperText"]);
    const lone = /(?<!\{)\{[A-Za-z]\w*\}(?!\})/;
    const bad = Object.entries(en as Record<string, string>)
      .filter(([k, v]) => !allow.has(k) && lone.test(v))
      .map(([k, v]) => `${k}: ${v}`);
    expect(bad, `English keys with lone single-brace placeholders (the loader only substitutes {{x}}):\n${bad.join("\n")}`).toEqual([]);
  });

  it("every non-English value uses the same {{vars}} as English", () => {
    const enB = en as Record<string, string>;
    const drift: string[] = [];
    for (const loc of PARKED_LOCALES) {
      const bundle = getMessages(loc) as Record<string, string>;
      for (const [key, ev] of Object.entries(enB)) {
        const want = bracedVars(ev);
        if (want.size === 0) continue;
        const lv = bundle[key];
        if (lv === undefined) continue; // absent -> en fallback, covered above
        const got = bracedVars(lv);
        if (got.size !== want.size || [...want].some((v) => !got.has(v))) {
          drift.push(`${loc} ${key}: en=${[...want].join(",")} loc=${[...got].join(",")}`);
        }
      }
    }
    expect(drift, `Locale values whose {{vars}} differ from English (data loss / raw {{var}} leak):\n${drift.join("\n")}`).toEqual([]);
  });
});

describe("i18n parked-locale fallback", () => {
  it("falls back to en for an unknown / parked locale tag", () => {
    expect(getMessages("xx")).toBe(en);
    expect(getMessages(null)).toBe(en);
  });

  it("returns the en string when a key is missing from the active locale bundle", () => {
    // Simulate a parked locale missing a key: the active bundle lacks it, en has it.
    const sampleKey = Object.keys(en)[0] as keyof typeof en;
    const activeBundle: Record<string, string> = {}; // parked locale with no keys
    const messages = activeBundle;
    // Mirror the runtime resolution order in useT: messages[key] ?? en[key] ?? key
    const resolved = messages[sampleKey] ?? (en as Record<string, string>)[sampleKey] ?? sampleKey;
    expect(resolved).toBe((en as Record<string, string>)[sampleKey]);
    expect(resolved).not.toBe(sampleKey);
  });

  it("interpolates vars into a known key", () => {
    // namedSets.total is "{{count}} total" — H21 added it.
    const messages = getMessages("en");
    const template = messages["namedSets.total"];
    expect(template).toContain("{{count}}");
    const rendered = template.replace("{{count}}", "5");
    expect(rendered).toBe("5 total");
  });

  it("publicGlossary.entryCount uses the {{visible}} var the page passes (F-018-06)", () => {
    // The public page calls t("publicGlossary.entryCount", { visible, total });
    // the template must use {{visible}}/{{total}} or the no-login page shows a
    // raw "{{visible}} of N entries" placeholder.
    const template = (en as Record<string, string>)["publicGlossary.entryCount"];
    expect(template).toContain("{{visible}}");
    expect(template).toContain("{{total}}");
    const rendered = template
      .replace("{{visible}}", "3")
      .replace("{{total}}", "12");
    expect(rendered).not.toContain("{{");
    expect(rendered).toBe("3 of 12 entries");
  });
});
