import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ImportWarning } from "../../api/importExportApi";
import ImportWarningAlerts from "./ImportWarningAlerts";

const t = (key: string) => key;

describe("Bug-8141 import warning panel parity", () => {
  it("renders info, warning, and error records at their declared severity", () => {
    const warnings: ImportWarning[] = (["info", "warning", "error"] as const)
      .map((severity) => ({
        code: `unknown.${severity}`,
        severity,
        source: "test",
        element: null,
        action: "review",
        params: {},
        detail: `${severity} detail`,
      }));

    render(<ImportWarningAlerts warnings={warnings} t={t} />);

    expect(screen.getAllByRole("alert").map(
      (alert) => alert.getAttribute("data-severity"),
    )).toEqual(["info", "warning", "error"]);
  });

  it("is the warning renderer wired by Project, dbt, Cube, and AtScale panels", () => {
    for (const panel of [
      "ProjectImportPanel.tsx",
      "DbtImportPanel.tsx",
      "CubeImportPanel.tsx",
      "AtScaleImportPanel.tsx",
    ]) {
      const source = readFileSync(resolve(
        process.cwd(), `src/components/importExport/panels/${panel}`,
      ), "utf8");
      expect(source, panel).toContain("<ImportWarningAlerts");
    }
  });
});
