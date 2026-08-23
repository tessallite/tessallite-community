import { createContext, useContext } from "react";

type Messages = Record<string, string>;

// ---------------------------------------------------------------------------
// English is eagerly loaded — it is the default and the fallback for every
// missing key in any other locale. Non-English locales are lazy-loaded on
// demand so the initial app chunk does not carry all eight locale bundles.
// ---------------------------------------------------------------------------

// Eager glob: only English domain files.
const enModules = import.meta.glob<Record<string, Messages>>(
  "./en/**/*.json",
  { eager: true, import: "default" },
);

// Merge all English domain files into a single flat bundle.
const enMessages: Messages = {};
for (const messages of Object.values(enModules)) {
  Object.assign(enMessages, messages);
}

// Lazy glob: every non-English locale. Vite emits each as a separate chunk.
const lazyModules = import.meta.glob<Record<string, Messages>>(
  ["./*/**/*.json", "!./en/**/*.json"],
  { import: "default" },
);

// In-memory cache of loaded locale bundles.
const bundles: Record<string, Messages> = { en: enMessages };

/**
 * Load a locale bundle on demand. Returns the merged flat bundle from cache
 * if already loaded. English is always synchronous.
 */
export async function loadLocale(locale: string): Promise<Messages> {
  const base = locale.split("-")[0];
  if (base === "en" || !base) return enMessages;
  if (bundles[base]) return bundles[base];

  // Collect all lazy module paths for this locale and load them in parallel.
  const entries = Object.entries(lazyModules).filter(
    ([path]) => path.split("/")[1] === base,
  );
  if (entries.length === 0) return enMessages;

  const loaded = await Promise.all(entries.map(([, loader]) => loader()));
  const merged: Messages = {};
  for (const mod of loaded) {
    Object.assign(merged, mod);
  }
  bundles[base] = merged;
  return merged;
}

// Backward-compat: a few error-boundary / test files import English
// messages directly (they run outside the I18nContext).
export default enMessages;

export const I18nContext = createContext<Messages>(enMessages);

/**
 * Synchronous accessor — returns the cached bundle for a locale if it has
 * been loaded via loadLocale(), otherwise falls back to English.
 */
export function getMessages(locale: string | null): Messages {
  if (!locale) return enMessages;
  const base = locale.split("-")[0];
  return bundles[base] ?? enMessages;
}

/** Locales whose script direction is right-to-left. */
export const RTL_LOCALES: ReadonlySet<string> = new Set(["ar"]);

export function useT(): (key: string, vars?: Record<string, string | number>) => string {
  const messages = useContext(I18nContext);
  return (key: string, vars?: Record<string, string | number>) => {
    let text = messages[key] ?? enMessages[key] ?? key;
    if (vars) {
      for (const [k, v] of Object.entries(vars)) {
        // Bug-6508: use split/join for global replacement so every occurrence
        // of a placeholder is interpolated, not just the first.
        text = text.split(`{{${k}}}`).join(String(v));
      }
    }
    return text;
  };
}
