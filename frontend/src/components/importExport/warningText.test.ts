import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { formatImportWarning } from "./warningText";
import type { ImportWarning } from "../../api/importExportApi";

const messages: Record<string, string> = {
  "importWarning.dbt.connection_placeholder":
    "Configure connection {{connection}} before querying.",
  "importWarning.diagnosticFallback": "Diagnostic {{code}}: {{detail}}",
  "importWarning.legacy": "Legacy: {{detail}}",
};

function t(key: string, vars?: Record<string, string | number>): string {
  let text = messages[key] ?? key;
  for (const [name, value] of Object.entries(vars ?? {})) {
    text = text.split(`{{${name}}}`).join(String(value));
  }
  return text;
}

describe("import warning translation boundary", () => {
  it("Bug-8141 translates a stable code and passes parameters, not raw JSON", () => {
    const warning: ImportWarning = {
      code: "dbt.connection_placeholder",
      severity: "error",
      source: "dbt",
      element: "dbt import",
      action: "configure",
      params: { connection: "dbt import" },
      detail: "SENTINEL backend diagnostic detail",
    };

    expect(formatImportWarning(warning, t)).toBe(
      "Configure connection dbt import before querying.",
    );
    expect(formatImportWarning(warning, t)).not.toContain("SENTINEL");
  });

  it("keeps diagnostic detail when a new code has no locale key", () => {
    const warning: ImportWarning = {
      code: "future.new_code",
      severity: "warning",
      source: "project_import",
      element: "definition",
      action: "review",
      params: {},
      detail: "Inspect this imported definition.",
    };

    expect(formatImportWarning(warning, t)).toBe(
      "Diagnostic future.new_code: Inspect this imported definition.",
    );
  });

  it("renders a legacy server string through the fallback key", () => {
    expect(formatImportWarning("old warning", t)).toBe("Legacy: old warning");
  });

  it("Bug-8141 never renders diagnostic detail for any known code", () => {
    const catalog = JSON.parse(readFileSync(resolve(
      process.cwd(), "../shared/importers/import_warning_catalog.json",
    ), "utf8")) as Record<string, {
      source: string;
      severity: ImportWarning["severity"];
      action: string;
      params: Record<string, "string" | "integer" | "string_list">;
      element_param?: string;
    }>;
    const locale = JSON.parse(readFileSync(resolve(
      process.cwd(), "src/i18n/en/importExport.json",
    ), "utf8")) as Record<string, string>;
    const translate = (key: string, vars?: Record<string, string | number>) => {
      let text = locale[key] ?? key;
      for (const [name, value] of Object.entries(vars ?? {})) {
        text = text.split(`{{${name}}}`).join(String(value));
      }
      return text;
    };

    for (const [code, spec] of Object.entries(catalog)) {
      const params = Object.fromEntries(Object.entries(spec.params).map(
        ([name, kind]) => [
          name,
          kind === "integer" ? 2 : kind === "string_list" ? ["one"] : name,
        ],
      ));
      const warning: ImportWarning = {
        code,
        severity: spec.severity,
        source: spec.source,
        element: spec.element_param ? String(params[spec.element_param]) : null,
        action: spec.action,
        params,
        detail: "BUG8141_SENTINEL_DIAGNOSTIC",
      };
      const rendered = formatImportWarning(warning, translate);
      expect(rendered, code).not.toContain("BUG8141_SENTINEL_DIAGNOSTIC");
      expect(rendered, code).not.toBe(`importWarning.${code}`);
    }
  });
});
