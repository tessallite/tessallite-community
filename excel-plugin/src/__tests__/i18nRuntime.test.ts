import { describe, expect, it, afterEach } from "vitest";
import { strings } from "../i18n/strings";
import { chatT } from "../i18n/chatStrings";
import { getActiveLocale, normaliseLocale, setActiveLocale } from "../i18n/runtime";

describe("Excel task-pane i18n runtime", () => {
  afterEach(() => {
    setActiveLocale("en");
  });

  it("normalises Office locale tags to base language codes", () => {
    expect(normaliseLocale("de-DE")).toBe("de");
    expect(normaliseLocale("fr_FR")).toBe("fr");
    expect(normaliseLocale(null)).toBe("en");
  });

  it("selects localized task-pane strings and falls back to English per key", () => {
    setActiveLocale("de-DE");

    expect(getActiveLocale()).toBe("de");
    expect(strings.app.askTessallite).toBe("Tessallite fragen");
    expect(strings.login.serverUrl).toBe("Server URL");
  });

  it("resolves task-pane strings from the active locale when consumers read them", () => {
    expect(strings.app.askTessallite).toBe("Ask Tessallite");

    setActiveLocale("de-DE");
    expect(strings.app.askTessallite).toBe("Tessallite fragen");

    setActiveLocale("fr-FR");
    expect(strings.app.askTessallite).toBe("Demander à Tessallite");
  });

  it("keeps unsupported Arabic task-pane RTL gated until its catalogue/theme ships (Bug-9211)", () => {
    setActiveLocale("ar-SA");
    expect(document.documentElement.lang).toBe("ar");
    // Arabic currently falls back to the English task-pane catalogue. Do not
    // mirror LTR MUI controls without the matching RTL theme and translations.
    expect(document.documentElement.dir).toBe("ltr");
    expect(strings.app.askTessallite).toBe("Ask Tessallite");

    setActiveLocale("fr-FR");
    expect(document.documentElement.lang).toBe("fr");
    expect(document.documentElement.dir).toBe("ltr");
  });

  it("localizes shared chat strings with interpolation and English fallback", () => {
    setActiveLocale("fr-FR");

    expect(chatT("composer.placeholder")).toBe("Posez une question sur vos données...");
    expect(chatT("steps.header", { n: 3 })).toBe("Étapes (3)");
    expect(chatT("badges.rows", { count: 12 })).toBe("12 lignes");
    expect(chatT("trace.routeWithValue", { route: "aggregate" })).toBe("Route: aggregate");
  });

  // R2-B04: an external cross-family review caught that these 7 keys (added
  // for Bug-7385/7549) were only added to chatStrings.ts's English fallback
  // map, never to runtime.ts's de/fr override maps, despite the L0 commit
  // claiming "en + de/fr". A de/fr task-pane user saw English for these
  // specific strings while every other overridden key was localized.
  it("localizes the Bug-7385/7549 shared-ui keys in German and French, not just English fallback (R2-B04)", () => {
    setActiveLocale("de-DE");
    expect(chatT("chart.measuresDimension")).toBe("Kennzahlen");
    expect(chatT("chart.rowLabel", { n: 3 })).toBe("Zeile 3");
    expect(chatT("chart.rowsDimension")).toBe("Zeilen");
    expect(chatT("chart.valueSeriesName")).toBe("Wert");
    expect(chatT("chat.scrollToBottomAria")).toBe("Nach unten scrollen");
    expect(chatT("queryBlock.copy")).toBe("Kopieren");
    expect(chatT("queryBlock.copied")).toBe("Kopiert");

    setActiveLocale("fr-FR");
    expect(chatT("chart.measuresDimension")).toBe("Mesures");
    expect(chatT("chart.rowLabel", { n: 3 })).toBe("Ligne 3");
    expect(chatT("chart.rowsDimension")).toBe("Lignes");
    expect(chatT("chart.valueSeriesName")).toBe("Valeur");
    expect(chatT("chat.scrollToBottomAria")).toBe("Défiler vers le bas");
    expect(chatT("queryBlock.copy")).toBe("Copier");
    expect(chatT("queryBlock.copied")).toBe("Copié");
  });

  it("tells the offline banner reader that health polling keeps running in the background (Bug-7390)", () => {
    // App.tsx polls every 30s (setInterval(runHealthPoll, 30000)); the old
    // "Retrying..." wording gave no indication the plugin was still trying
    // rather than having silently given up.
    expect(strings.connection.lost).toMatch(/30 seconds/);
  });

  // GPT Phase 4 review ENHANCEMENT on Bug-7390: the English assertion above
  // does not protect the German/French runtime.ts overrides — a regression
  // that updated the English cadence wording but left either override stale
  // would go uncaught. Both carry the same "30" cadence in their own language.
  it("keeps the German and French offline-banner overrides on the same 30-second cadence as English (Bug-7390)", () => {
    setActiveLocale("de-DE");
    expect(strings.connection.lost).toMatch(/30/);

    setActiveLocale("fr-FR");
    expect(strings.connection.lost).toMatch(/30/);
  });
});
