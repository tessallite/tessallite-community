import type { ImportWarning } from "../../api/importExportApi";

export type ImportWarningTranslator = (
  key: string,
  vars?: Record<string, string | number>,
) => string;

/** Render a structured import warning through the locale bundle. */
export function formatImportWarning(
  warning: ImportWarning | string,
  t: ImportWarningTranslator,
): string {
  if (typeof warning === "string") {
    return t("importWarning.legacy", { detail: warning });
  }

  const knownVars: Record<string, string | number> = {
    source: warning.source,
    code: warning.code,
  };
  for (const [key, value] of Object.entries(warning.params ?? {})) {
    knownVars[key] = Array.isArray(value) ? value.join(", ") : String(value);
  }

  const key = `importWarning.${warning.code}`;
  const translated = t(key, knownVars);
  return translated === key
    ? t("importWarning.diagnosticFallback", {
        code: warning.code,
        detail: warning.detail,
      })
    : translated;
}

export function importWarningSeverity(
  warning: ImportWarning | string,
): "info" | "warning" | "error" {
  return typeof warning === "string" ? "warning" : warning.severity;
}
