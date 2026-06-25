import { useEffect } from "react";
import { useBuilderStore } from "../store/builderStore";

const STORAGE_KEY = "user_language_preference";

export function useLanguagePreference() {
  const setDisplayLocale = useBuilderStore((s) => s.setDisplayLocale);

  useEffect(() => {
    try {
      const savedLanguage = localStorage.getItem(STORAGE_KEY);
      if (savedLanguage) {
        setDisplayLocale(savedLanguage);
      }
    } catch (error) {
      console.warn("Failed to read language preference from localStorage:", error);
    }
  }, [setDisplayLocale]);
}
