// Bug-7232: the fail-loud backend-authored status labels are fixed English
// sentences; localizeKpiLabel must map every known one to an i18n key while
// custom band labels pass through verbatim (authored copy is never rewritten).
import { describe, it, expect } from "vitest";
import { localizeKpiLabel } from "./statusUtils";

const t = (key: string): string => `‹${key}›`;

describe("localizeKpiLabel backend fail-loud labels (Bug-7232)", () => {
  it.each([
    ["Restricted by row security", "‹kpiScorecard.statusRowSecurityRestricted›"],
    [
      "Model is not deployed — deploy the model before evaluating KPIs",
      "‹kpiScorecard.modelNotDeployed›",
    ],
    [
      "Evaluation failed — this time-intelligence KPI needs a time dimension",
      "‹kpiScorecard.tiNeedsTimeDimension›",
    ],
    [
      "Composite evaluation failed — circular composite reference",
      "‹kpiScorecard.compositeCycle›",
    ],
    [
      "Evaluation failed — check KPI expression and model scope",
      "‹kpiScorecard.evaluationFailedGeneric›",
    ],
    ["Target query failed", "‹kpiScorecard.targetQueryFailed›"],
    ["No expression configured", "‹kpiScorecard.noExpression›"],
    [
      "Evaluation failed — every child KPI errored",
      "‹kpiScorecard.allChildrenErrored›",
    ],
  ])("maps %s", (label, key) => {
    expect(localizeKpiLabel(label, t)).toBe(key);
  });

  it("maps the parameterised composite-depth label via its prefix", () => {
    expect(
      localizeKpiLabel(
        "Composite evaluation failed — composite nesting deeper than 4 levels",
        t,
      ),
    ).toBe("‹kpiScorecard.compositeDepthExceeded›");
  });

  it("passes custom band labels through verbatim", () => {
    expect(localizeKpiLabel("Breach watch OK", t)).toBe("Breach watch OK");
    expect(localizeKpiLabel("  ", t)).toBe("  ");
    expect(localizeKpiLabel(null, t)).toBeNull();
    expect(localizeKpiLabel(undefined, t)).toBeNull();
  });
});
