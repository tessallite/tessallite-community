import { RTL_LOCALES } from "../i18n";

export type ThemeDirection = "ltr" | "rtl";

export function localeDirection(locale: string | null): ThemeDirection {
  const lang = locale ? locale.split("-")[0] : "en";
  return RTL_LOCALES.has(lang) ? "rtl" : "ltr";
}
