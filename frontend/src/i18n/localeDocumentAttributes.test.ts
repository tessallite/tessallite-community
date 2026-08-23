import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useEffect } from "react";
import { useBuilderStore } from "../store/builderStore";
import { RTL_LOCALES } from "./index";

// Bug-6509: verify that switching locale updates <html lang> and <html dir>.
//
// Rather than rendering the full RootShell (which needs a router, providers,
// etc.), this test exercises the same logic that RootShell's useEffect applies:
// reading displayLocale from the store and setting document.documentElement
// attributes. This is a behavioral test of the contract, not the component
// wiring -- the deep-review verifies the wiring in App.tsx.

/** Minimal hook that mirrors the RootShell locale-attribute effect. */
function useLocaleDocumentAttributes() {
  const displayLocale = useBuilderStore((s) => s.displayLocale);
  useEffect(() => {
    const lang = displayLocale ? displayLocale.split("-")[0] : "en";
    document.documentElement.lang = lang;
    document.documentElement.dir = RTL_LOCALES.has(lang) ? "rtl" : "ltr";
  }, [displayLocale]);
}

describe("Bug-6509: locale document attributes (lang + dir)", () => {
  beforeEach(() => {
    localStorage.clear();
    act(() => useBuilderStore.getState().reset());
    // Reset to initial state.
    document.documentElement.lang = "en";
    document.documentElement.dir = "";
  });

  afterEach(() => {
    localStorage.clear();
    document.documentElement.lang = "en";
    document.documentElement.dir = "";
  });

  it("sets lang='en' and dir='ltr' for the default (null) locale", () => {
    renderHook(() => useLocaleDocumentAttributes());

    expect(document.documentElement.lang).toBe("en");
    expect(document.documentElement.dir).toBe("ltr");
  });

  it("sets lang='fr' and dir='ltr' for French", () => {
    act(() => useBuilderStore.getState().setDisplayLocale("fr"));
    renderHook(() => useLocaleDocumentAttributes());

    expect(document.documentElement.lang).toBe("fr");
    expect(document.documentElement.dir).toBe("ltr");
  });

  it("sets lang='ar' and dir='rtl' for Arabic", () => {
    act(() => useBuilderStore.getState().setDisplayLocale("ar"));
    renderHook(() => useLocaleDocumentAttributes());

    expect(document.documentElement.lang).toBe("ar");
    expect(document.documentElement.dir).toBe("rtl");
  });

  it("switches from Arabic RTL back to English LTR", () => {
    act(() => useBuilderStore.getState().setDisplayLocale("ar"));
    const { rerender } = renderHook(() => useLocaleDocumentAttributes());

    expect(document.documentElement.dir).toBe("rtl");

    act(() => useBuilderStore.getState().setDisplayLocale(null));
    rerender();

    expect(document.documentElement.lang).toBe("en");
    expect(document.documentElement.dir).toBe("ltr");
  });

  it("sets lang='ja' and dir='ltr' for Japanese", () => {
    act(() => useBuilderStore.getState().setDisplayLocale("ja"));
    renderHook(() => useLocaleDocumentAttributes());

    expect(document.documentElement.lang).toBe("ja");
    expect(document.documentElement.dir).toBe("ltr");
  });

  it("handles region-tag locales like 'fr-CA' by extracting base language", () => {
    act(() => useBuilderStore.getState().setDisplayLocale("fr-CA"));
    renderHook(() => useLocaleDocumentAttributes());

    expect(document.documentElement.lang).toBe("fr");
    expect(document.documentElement.dir).toBe("ltr");
  });
});
