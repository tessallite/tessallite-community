import { describe, it, expect } from "vitest";
import { EXPRESSION_TEMPLATES } from "./expressionTemplates";

describe("expressionTemplates", () => {
  it("defines 9 templates", () => {
    expect(EXPRESSION_TEMPLATES).toHaveLength(9);
  });

  it("every template has required fields", () => {
    for (const tpl of EXPRESSION_TEMPLATES) {
      expect(tpl.id).toBeTruthy();
      expect(tpl.labelKey).toBeTruthy();
      expect(tpl.labelFallback).toBeTruthy();
      expect(tpl.expression).toBeTruthy();
      expect(tpl.placeholderCount).toBeGreaterThan(0);
    }
  });

  it("has no duplicate IDs", () => {
    const ids = EXPRESSION_TEMPLATES.map((t) => t.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("placeholder count matches actual ? count in expression", () => {
    for (const tpl of EXPRESSION_TEMPLATES) {
      const questionMarks = (tpl.expression.match(/\?/g) || []).length;
      expect(questionMarks).toBe(tpl.placeholderCount);
    }
  });

  it("all expressions use valid DSL syntax patterns", () => {
    for (const tpl of EXPRESSION_TEMPLATES) {
      // Every template should contain at least one function call or measure reference
      expect(tpl.expression).toMatch(/[a-z_]+\(/);
    }
  });
});
