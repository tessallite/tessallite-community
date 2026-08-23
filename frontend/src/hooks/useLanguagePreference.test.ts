import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useLanguagePreference } from "./useLanguagePreference";
import { useBuilderStore } from "../store/builderStore";

// Bug-5984: locale persistence previously used two storage keys
// (`user_language_preference` written by LocaleSelector,
// `display_locale` written by the builder store) with different English
// semantics -- selecting English wrote the string "en" to one key while
// the store's own convention was `null` for base English. These tests
// pin the single-key, single-semantic contract: `display_locale` only,
// `null`/absent = base English.
describe("useLanguagePreference", () => {
  beforeEach(() => {
    localStorage.clear();
    act(() => useBuilderStore.getState().reset());
  });

  afterEach(() => {
    localStorage.clear();
  });

  it("hydrates displayLocale from the display_locale key on mount", () => {
    localStorage.setItem("display_locale", "fr");

    renderHook(() => useLanguagePreference());

    expect(useBuilderStore.getState().displayLocale).toBe("fr");
  });

  it("leaves displayLocale at base English when no key is stored", () => {
    renderHook(() => useLanguagePreference());

    expect(useBuilderStore.getState().displayLocale).toBeNull();
  });

  it("does not read the legacy user_language_preference key", () => {
    localStorage.setItem("user_language_preference", "de");

    renderHook(() => useLanguagePreference());

    expect(useBuilderStore.getState().displayLocale).toBeNull();
  });

  it("selecting a non-English locale via setDisplayLocale persists to display_locale only", () => {
    act(() => useBuilderStore.getState().setDisplayLocale("es"));

    expect(localStorage.getItem("display_locale")).toBe("es");
    expect(localStorage.getItem("user_language_preference")).toBeNull();
  });

  it("selecting English via setDisplayLocale(null) clears display_locale rather than storing a value", () => {
    act(() => useBuilderStore.getState().setDisplayLocale("es"));
    act(() => useBuilderStore.getState().setDisplayLocale(null));

    expect(localStorage.getItem("display_locale")).toBeNull();
    expect(useBuilderStore.getState().displayLocale).toBeNull();
  });
});
