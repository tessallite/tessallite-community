import { useEffect } from "react";
import { useBuilderStore } from "../store/builderStore";

/**
 * Reads the persisted locale from the canonical ``display_locale``
 * localStorage key and hydrates the builder store on mount. The store
 * itself already bootstraps from the same key (see builderStore.ts),
 * but this hook is kept for callers that mount after the initial
 * static bootstrap.
 */
export function useLanguagePreference() {
  const setDisplayLocale = useBuilderStore((s) => s.setDisplayLocale);

  useEffect(() => {
    try {
      const savedLanguage = localStorage.getItem("display_locale");
      if (savedLanguage) {
        setDisplayLocale(savedLanguage);
      }
    } catch (error) {
      console.warn("Failed to read language preference from localStorage:", error);
    }
  }, [setDisplayLocale]);
}
