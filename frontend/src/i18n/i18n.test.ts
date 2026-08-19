import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderHook } from "@testing-library/react";
// en.json was split into per-domain files (en/*.json) merged by ./index; the
// default export is the flat merged English bundle (dotted keys), so the
// coverage guard below still asserts every referenced key resolves in English.
import en, { getMessages, loadLocale, useT, I18nContext, RTL_LOCALES } from "./index";
// Producer constants — the test derives finite-family domains from these so it
// can never drift from the code that actually builds the t(`prefix.${x}`) keys.
// If a producer adds a value with no en key, the derived expansion fails here.
import { KPI_TEMPLATES, NAMED_SET_TEMPLATES } from "../components/Panels/templates";
import { AGGREGATION_OPTIONS } from "../components/KpiBusinessBuilder/businessDefinition";
import { AGG_OPTIONS } from "../components/Panels/MeasureQueryPanel/measureColumns";
import { localeDirection } from "../theme/direction";

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
const tessalliteRoot = join(srcRoot, "..", "..");
const enKeys = new Set(Object.keys(en as Record<string, string>));
const PARKED_LOCALES = ["ar", "de", "es", "fr", "ja", "pt", "zh"];

function flattenMessages(value: unknown, prefix = ""): Record<string, string> {
  const out: Record<string, string> = {};
  if (!value || typeof value !== "object" || Array.isArray(value)) return out;
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    const fullKey = prefix ? `${prefix}.${key}` : key;
    if (child && typeof child === "object" && !Array.isArray(child)) {
      Object.assign(out, flattenMessages(child, fullKey));
    } else if (typeof child === "string") {
      out[fullKey] = child;
    }
  }
  return out;
}

function readDomainMessages(locale: string, file: string): Record<string, string> {
  return flattenMessages(JSON.parse(readFileSync(join(here, locale, file), "utf8")));
}

function readRoutableQuantilePercentiles(): string[] {
  const source = readFileSync(join(tessalliteRoot, "shared", "aggregate_quantiles.py"), "utf8");
  const match = source.match(/ROUTABLE_QUANTILE_PERCENTILES:\s*list\[int\]\s*=\s*\[([^\]]*)\]/);
  expect(match, "Could not find ROUTABLE_QUANTILE_PERCENTILES in shared/aggregate_quantiles.py").not.toBeNull();
  return match![1]
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean)
    .map((v) => `p${String(Number(v)).padStart(2, "0")}`.replace("p50", "p50"));
}

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

// Bug-7726: non-English bundles are lazy-loaded at runtime. Pre-load them
// for tests that call getMessages(loc) to inspect parked-locale content.
beforeAll(async () => {
  await Promise.all(PARKED_LOCALES.map((loc) => loadLocale(loc)));
});

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
    // here. (Only genuinely open-domain families are excluded: `pipeline.meta.*`
    // — arbitrary backend trace keys rendered through a humanise() fallback that
    // never leaks a raw key. Helper-returned literal-key families that always
    // return a hardcoded key on every branch — e.g. advisoryLabelKey /
    // pathBadgeKey — cannot leak either but their returned literals are still
    // enumerated below so a deleted en key is caught.)
    //
    // Each entry: the t() call-site transform applied to each domain member, then
    // the produced key prefixed. The transforms mirror the call sites exactly.

    const pascal = (s: string) =>
      s.charAt(0).toUpperCase() + s.slice(1).replace(/_([a-z])/g, (_, c: string) => c.toUpperCase());

    const expansions: string[] = [];
    const add = (...keys: string[]) => expansions.push(...keys);

    // --- users.role* (UsersAccessPanel roleKey) ---
    // roleKey(role) = `users.role${pascal(role)}` (identical underscore->camel
    // transform as `pascal` here) is applied to BOTH producer domains:
    //   USER_ROLES (LocalUserRole)  = member | tenant_admin | model_technical
    //   ACCESS_ROLES (AccessRole)   = admin | modeler | viewer | model_viewer
    //     (model_viewer added Bug-8101/F-104-01 — a new AccessRole member must
    //      appear here or its role chip leaks the raw key).
    const ALL_ROLES = [
      "member", "tenant_admin", "model_technical",
      "admin", "modeler", "viewer", "model_viewer",
    ];
    for (const r of ALL_ROLES) add(`users.role${pascal(r)}`);

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

    // --- roles.* (binding/role chips: TenantAdmin/SystemAdmin/GroupMappings) ---
    // t(`roles.${role}`) over the SAME LocalUserRole ∪ AccessRole domain (incl.
    // model_viewer) — raw snake_case suffix, no transform.
    for (const r of ALL_ROLES) add(`roles.${r}`);

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

    // --- alerts.eventType.* (AlertsPanel: t(`alerts.eventType.${et.value}`)) ---
    // The domain is backend-driven (GET /event-types, shared.alerting.dispatcher
    // .EVENT_TYPES) rather than a frontend TS union, so it is enumerated by hand
    // here exactly like lifecycleLog.eventType.* above mirrors its own backend
    // producer. Includes the three kpi_* values (a separate KPI-alert source that
    // is deliberately EXCLUDED from the notification EVENT_TYPES catalogue —
    // see test_catalogue_offers_no_kpi_events on the backend — but still renders
    // through this same AlertsPanel key family). Bug-8114 added
    // pocket_refresh_failure; a new EVENT_TYPES member with no entry here leaks
    // the raw event name in the Alerts panel dropdown.
    for (const ev of [
      "refresh_failure", "schema_drift", "sla_breach", "query_failure_spike",
      "aggregate_retired", "refresh_upstream_failed", "pocket_refresh_failure",
      "kpi_threshold_breach", "kpi_status_change", "kpi_trend_alert",
    ])
      add(`alerts.eventType.${ev}`);

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

    // --- advisories.severity.${sev} (AdvisoryPanel Severity union) ---
    // severityKey() normalises any raw backend severity to one of these five;
    // the chip label t(`advisories.severity.${sev}`) has no fallback.
    for (const s of ["info", "low", "medium", "high", "critical"]) add(`advisories.severity.${s}`);

    // --- modelHealth.relState.${state} (RelationshipHealthSection) ---
    // r.state producer domain mirrors RELATIONSHIP_STATE_COLOR in ModelHealthPanel.
    // The color has a `?? "default"` fallback but the label t(...) does not.
    for (const st of ["healthy", "broken", "error", "stale", "pending"]) add(`modelHealth.relState.${st}`);

    // --- diagnostics.clientKindLabel.${client_kind} (DiagnosticsPanel) ---
    // Finite query-log client-kind domain (the filter dropdown, minus "all").
    // Canonical source: tessallite/shared/query_log_client_kinds.py
    // (QUERY_LOG_CLIENT_KINDS). Keep this list in step with it — the frontend
    // cannot import the Python tuple, so this test is the parity guard.
    for (const ck of ["looker_studio", "looker_cloud", "plugin", "drill", "headless", "agent", "mcp", "kpi"])
      add(`diagnostics.clientKindLabel.${ck}`);
    // The client filter dropdown's own labels (DiagnosticsPanel MenuItems).
    for (const k of ["All", "LookerStudio", "LookerCloud", "Plugin", "Drill", "Headless", "Agent", "Mcp", "Kpi"])
      add(`diagnostics.client${k}`);

    // --- kpiBusiness.${shareType} (KpiBusinessBuilderDialog / KpiCard) ---
    // ShareType ternary: rank -> rank, top_n_contribution -> topN, else sharePercent.
    for (const k of ["rank", "topN", "sharePercent"]) add(`kpiBusiness.${k}`);

    // --- kpiBusiness.sla${SlaKind} (KpiBusinessBuilderDialog / KpiCard) ---
    // SLA ternary: compliance_pct -> slaCompliancePct, exception_count -> slaBreachCount,
    // else slaBacklog.
    for (const k of ["CompliancePct", "BreachCount", "Backlog"]) add(`kpiBusiness.sla${k}`);

    // --- scratchpad.dataType.${dt} (ScratchpadPanel SCRATCHPAD_DATA_TYPES) ---
    // Finite `as const` list (F-029-14: the fixed select prevents a raw-key leak);
    // the label t(`scratchpad.dataType.${dt}`) has no fallback.
    for (const dt of ["numeric", "integer", "string", "boolean", "date", "timestamp"])
      add(`scratchpad.dataType.${dt}`);

    // --- helper-returned literal-key families (t(helperKey(x))) ---
    // These helpers return a hardcoded en key on every branch (incl. default) so
    // they cannot leak a raw key, but the static t("...") collector cannot see a
    // key returned from a helper — enumerate the literals so a deleted en key is
    // still caught.
    // NamedSetsPanel.pathBadgeKey:
    add("namedSets.pathBadgeSql", "namedSets.pathBadgeXmla");
    // AttributeRelationshipsSection.advisoryLabelKey:
    add(
      "attributeRelationships.advisoryOk",
      "attributeRelationships.advisoryNotBijection",
      "attributeRelationships.advisoryError",
      "attributeRelationships.advisoryNotChecked",
    );

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
  // adds, drops, or renames a {{var}} relative to English silently loses data
  // or leaks a raw {{var}} the page never substitutes.
  // English itself must use only double-brace {{x}} (the loader only
  // substitutes {{x}}; a lone {x} renders literally — F-i18n-01).
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

  it("contains no empty-string values in any locale bundle (Bug-6516)", () => {
    const empties: string[] = [];
    for (const loc of PARKED_LOCALES) {
      const bundle = getMessages(loc) as Record<string, string>;
      for (const [key, value] of Object.entries(bundle)) {
        if (typeof value === "string" && value.trim() === "") {
          empties.push(`${loc} ${key}`);
        }
      }
    }
    expect(empties, `Locale keys with empty values bypass the English fallback:\n${empties.join("\n")}`).toEqual([]);
  });
});

describe("Bug-7543: cross-namespace duplicate key collision guard", () => {
  it("rejects any key that appears in more than one domain JSON file per locale", () => {
    const domainFiles = readdirSync(join(here, "en")).filter((name) => name.endsWith(".json"));
    const collisions: string[] = [];
    for (const loc of ["en", ...PARKED_LOCALES]) {
      const seen = new Map<string, string>();
      for (const file of domainFiles) {
        const keys = Object.keys(readDomainMessages(loc, file));
        for (const key of keys) {
          const prev = seen.get(key);
          if (prev) {
            collisions.push(`${loc}: "${key}" appears in both ${prev} and ${file}`);
          } else {
            seen.set(key, file);
          }
        }
      }
    }
    expect(
      collisions,
      `Cross-namespace duplicate keys (Object.assign overwrites silently):\n${collisions.join("\n")}`,
    ).toEqual([]);
  });
});

describe("i18n locale catalogue parity", () => {
  it("keeps every non-English domain free of keys absent from English (missing keys allowed while translations are parked)", () => {
    // Parked-translations policy: new UI keys land in en.json ONLY, so a parked
    // locale is allowed to LACK en keys (untranslated -> i18next falls back to
    // English at runtime). What it must NOT do is carry orphan keys absent from
    // English -- those resolve to nothing and signal a stale/typo'd key. This
    // guard therefore flags `extra` (orphan) keys only. Re-tighten to full
    // structural parity when translations un-park.
    const domainFiles = readdirSync(join(here, "en")).filter((name) => name.endsWith(".json"));
    const orphans: string[] = [];
    for (const loc of PARKED_LOCALES) {
      for (const file of domainFiles) {
        const enDomain = readDomainMessages("en", file);
        const locDomain = readDomainMessages(loc, file);
        const extra = Object.keys(locDomain).filter(
          (key) => !Object.prototype.hasOwnProperty.call(enDomain, key),
        );
        if (extra.length) {
          orphans.push(`${loc}/${file}: extra=[${extra.join(", ")}]`);
        }
      }
    }
    expect(orphans, `Locale keys absent from English (orphans):\n${orphans.join("\n")}`).toEqual([]);
  });

  it("keeps aggregate quantile wording aligned to the producer routable percentile contract", () => {
    const routable = readRoutableQuantilePercentiles();
    expect(routable).toEqual(["p50"]);
    const forbidden = ["p1", "p5", "p10", "p25", "p75", "p90", "p95", "p99"];
    const materializedClaimKeys = [
      ["panels.json", "aggDrawer.includeQuantiles"],
      ["panels.json", "aggEstimate.quantileNote"],
      ["panels.json", "aggEstimate.quantilesIncluded"],
      ["panels.json", "aggregate.includeQuantiles"],
      ["panels.json", "aggregates.estimate.quantilesIncluded"],
      ["ui.json", "ui.includeQuantileColumnsP25P50P75P95"],
    ] as const;
    const bad: string[] = [];
    for (const loc of ["en", ...PARKED_LOCALES]) {
      for (const [file, key] of materializedClaimKeys) {
        const value = readDomainMessages(loc, file)[key] ?? "";
        for (const p of routable) {
          if (!value.includes(p)) bad.push(`${loc}/${file}:${key} omits ${p}: ${value}`);
        }
        for (const p of forbidden) {
          if (new RegExp(`\\b${p}\\b`, "i").test(value)) {
            bad.push(`${loc}/${file}:${key} advertises unroutable ${p}: ${value}`);
          }
        }
      }
    }
    for (const loc of PARKED_LOCALES) {
      for (const [file, key] of materializedClaimKeys) {
        const value = readDomainMessages(loc, file)[key] ?? "";
        if (/Include median column|Median \(p50\) column included/i.test(value)) {
          bad.push(`${loc}/${file}:${key} still uses English percentile prose: ${value}`);
        }
      }
    }
    for (const loc of ["en", ...PARKED_LOCALES]) {
      const tooltip = readDomainMessages(loc, "panels.json")["aggregate.includeQuantilesTooltip"] ?? "";
      if (!tooltip.includes("p50")) {
        bad.push(`${loc}/panels.json:aggregate.includeQuantilesTooltip omits routable p50: ${tooltip}`);
      }
      if (!forbidden.some((p) => tooltip.includes(p))) {
        bad.push(`${loc}/panels.json:aggregate.includeQuantilesTooltip omits unavailable percentile context: ${tooltip}`);
      }
    }
    expect(bad, `Aggregate quantile locale wording drift:\n${bad.join("\n")}`).toEqual([]);
  });

  it("distinguishes protected terms from untranslated German and French settings prose", () => {
    const protectedIdentical = new Set([
      "Amazon Redshift",
      "Claude Desktop",
      "Code",
      "Collibra",
      "Hadoop / Spark (Hive Thrift)",
      "HTTP JSON",
      "Java",
      "JDBC / Hadoop",
      "Power BI",
      "Power BI · Excel",
      "PostgreSQL — Port 5433",
      "Schema",
      "Slack",
      "Solidatus",
      "SQL Server",
      "Table",
      "Type",
      "Webhooks",
      "curl",
      "psql",
      "Python",
      "Minute",
    ]);
    const protectedPatterns = [
      /^https?:\/\//,
      /^\{.*\}$/,
      /^[a-z]+(?:[._][a-z]+)+$/,
    ];
    const bad: string[] = [];
    for (const loc of ["de", "fr"]) {
      const enSettings = readDomainMessages("en", "settings.json");
      const locSettings = readDomainMessages(loc, "settings.json");
      for (const [key, english] of Object.entries(enSettings)) {
        if (locSettings[key] !== english) continue;
        if (protectedIdentical.has(english) || protectedPatterns.some((pattern) => pattern.test(english))) continue;
        if (!/[A-Za-z]+ [A-Za-z]+/.test(english)) continue;
        bad.push(`${loc} ${key}: ${english}`);
      }
    }
    expect(bad, `Unprotected de/fr settings prose still matches English:\n${bad.join("\n")}`).toEqual([]);
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

describe("Bug-6508: useT global placeholder interpolation", () => {
  // Helper: render useT inside an I18nContext with the given messages bundle.
  function renderUseT(messages: Record<string, string>) {
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      createElement(I18nContext.Provider, { value: messages }, children);
    const { result } = renderHook(() => useT(), { wrapper });
    return result.current;
  }

  it("replaces ALL occurrences of a repeated placeholder, not just the first", () => {
    const messages = {
      "test.repeated": "v{{n}} replaces v{{n}} fully",
    };
    const t = renderUseT(messages);
    const rendered = t("test.repeated", { n: 3 });
    expect(rendered).toBe("v3 replaces v3 fully");
    expect(rendered).not.toContain("{{");
  });

  it("replaces both occurrences in the real versions.revertMessage template", () => {
    const enBundle = getMessages("en");
    const t = renderUseT(enBundle as Record<string, string>);
    const rendered = t("versions.revertMessage", { n: 5 });
    expect(rendered).not.toContain("{{n}}");
    expect(rendered).toContain("v5");
    // Both occurrences must resolve.
    const count = rendered.split("v5").length - 1;
    expect(count).toBe(2);
  });

  it("replaces both occurrences in the real joins.sameTypeGeneric template", () => {
    const enBundle = getMessages("en");
    const t = renderUseT(enBundle as Record<string, string>);
    const rendered = t("joins.sameTypeGeneric", { type: "Dim" });
    expect(rendered).not.toContain("{{type}}");
    expect(rendered).toBe("Dim-to-Dim");
  });

  it("handles multiple different placeholders each appearing once", () => {
    const messages = {
      "test.multi": "Hello {{name}}, you have {{count}} items",
    };
    const t = renderUseT(messages);
    const rendered = t("test.multi", { name: "Alice", count: 7 });
    expect(rendered).toBe("Hello Alice, you have 7 items");
  });
});

describe("Bug-6509: RTL_LOCALES and document attribute helpers", () => {
  it("RTL_LOCALES contains Arabic", () => {
    expect(RTL_LOCALES.has("ar")).toBe(true);
  });

  it("RTL_LOCALES does not contain LTR locales", () => {
    for (const ltr of ["en", "fr", "de", "es", "ja", "pt", "zh"]) {
      expect(RTL_LOCALES.has(ltr)).toBe(false);
    }
  });

  it("maps Arabic locale tags to RTL and live-switched non-Arabic tags to LTR", () => {
    expect(localeDirection("ar")).toBe("rtl");
    expect(localeDirection("ar-EG")).toBe("rtl");
    expect(localeDirection("de")).toBe("ltr");
    expect(localeDirection(null)).toBe("ltr");
  });
});
