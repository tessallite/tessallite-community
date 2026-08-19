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

  it("updates document language and direction for Office task pane locale changes", () => {
    setActiveLocale("ar-SA");
    expect(document.documentElement.lang).toBe("ar");
    expect(document.documentElement.dir).toBe("rtl");

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
});
