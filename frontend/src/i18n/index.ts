import { createContext, useContext } from "react";

type Messages = Record<string, string>;

// Each language is now a directory of domain files (e.g. en/common.json,
// en/agent.json, ...).  Vite merges them at build time via glob import.
const langModules = import.meta.glob<Record<string, Messages>>(
  "./*/**/*.json",
  { eager: true, import: "default" },
);

// Group module defaults by language code (the first directory segment).
const bundles: Record<string, Messages> = {};
for (const [path, messages] of Object.entries(langModules)) {
  const lang = path.split("/")[1]; // e.g. "./en/common.json" -> "en"
  bundles[lang] ??= {};
  Object.assign(bundles[lang], messages);
}

// Fallback bundle — English.
const enMessages: Messages = bundles["en"] ?? {};

// Backward-compat: a few error-boundary / test files import English
// messages directly (they run outside the I18nContext).
export default enMessages;

export const I18nContext = createContext<Messages>(enMessages);

export function getMessages(locale: string | null): Messages {
  if (!locale) return enMessages;
  const base = locale.split("-")[0];
  return bundles[base] ?? enMessages;
}

export function useT(): (key: string, vars?: Record<string, string | number>) => string {
  const messages = useContext(I18nContext);
  return (key: string, vars?: Record<string, string | number>) => {
    let text = messages[key] ?? enMessages[key] ?? key;
    if (vars) {
      for (const [k, v] of Object.entries(vars)) {
        text = text.replace(`{{${k}}}`, String(v));
      }
    }
    return text;
  };
}
