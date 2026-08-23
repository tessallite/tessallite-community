import { describe, it, expect } from "vitest";
import panels from "../../i18n/en/panels.json";

// Bug-7040: the Row Security editor's predicate guidance must describe the
// restricted DSL the backend actually accepts — NOT raw SQL. The backend
// (shared/security/predicate_compiler.py::_compile_dsl_expression, enforced at
// save time by _validate_predicate_compiles) accepts ONLY the functions
// dimension_equals / in / and / or / not with single-quoted string values, and
// rejects anything else (including a bare `col = value` SQL comparison and the
// old `{{claim}}` template token). These guards keep the placeholder + helper
// text aligned with that grammar so the editor stops instructing users to enter
// syntax the backend rejects.

const strings = panels as Record<string, string>;

// The DSL function names the backend recognises. If the compiler grammar
// changes, this list and the guidance must change together.
const DSL_FUNCTIONS = ["dimension_equals", "in", "and", "or", "not"];

describe("Row Security predicate guidance (Bug-7040)", () => {
  it("placeholder is a valid DSL function call, not a raw SQL comparison", () => {
    const placeholder = strings["rowSecurity.predicatePlaceholder"];
    expect(placeholder).toBeTruthy();
    // Must use a recognised DSL function...
    expect(placeholder).toMatch(/dimension_equals\(|in\(|and\(|or\(|not\(/);
    // ...and must NOT resemble the old raw-SQL example the backend rejects.
    expect(placeholder).not.toContain("{{claim}}");
    // A bare `col = 'value'` SQL comparison (no function call) is rejected by
    // the DSL parser; the placeholder must not model one.
    expect(placeholder).not.toMatch(/^[^(]*=\s*'/);
  });

  it("helper text names the DSL functions and does not call itself raw SQL", () => {
    const helper = strings["rowSecurity.predicateHelperText"];
    expect(helper).toBeTruthy();
    // Every accepted DSL function is mentioned so users know the vocabulary.
    for (const fn of DSL_FUNCTIONS) {
      expect(helper).toContain(fn);
    }
    // The previous helper called it a "SQL predicate", which is what misled
    // users into typing rejected raw SQL.
    expect(helper).not.toContain("SQL predicate");
  });
});
