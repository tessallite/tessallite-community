/**
 * Bug-7661 regression tests: multi_value default round-trip.
 *
 * The ParametersPanel formats stored defaults for editing (formatDefaultValue)
 * and parses the text field back on save (parseDefaultValue). For multi_value
 * parameters the stored value is an array (e.g. ["EMEA", "NA"]). Before the
 * fix, formatDefaultValue JSON-stringified it ('["EMEA","NA"]'), but
 * parseDefaultValue comma-split the text, producing corrupted values like
 * '["EMEA"' and '"NA"]'. Each edit->save round-trip accumulated more
 * corruption.
 *
 * After the fix, formatDefaultValue joins arrays with ", " and parseDefaultValue
 * splits on ",", so the round-trip is lossless.
 */
import { describe, it, expect } from "vitest";
import {
  parseDefaultValue,
  formatDefaultValue,
  defaultValueError,
  PARAM_NAME_RE,
} from "./ParametersPanel";

describe("Bug-7661: multi_value default round-trip", () => {
  it("formatDefaultValue renders array as comma-separated text", () => {
    expect(formatDefaultValue(["EMEA", "NA", "APAC"])).toBe("EMEA, NA, APAC");
  });

  it("parseDefaultValue splits comma-separated text into array", () => {
    expect(parseDefaultValue("EMEA, NA, APAC", "multi_value")).toEqual([
      "EMEA",
      "NA",
      "APAC",
    ]);
  });

  it("multi_value default survives format->parse round-trip", () => {
    const original = ["EMEA", "NA", "APAC"];
    const formatted = formatDefaultValue(original);
    const parsed = parseDefaultValue(formatted, "multi_value");
    expect(parsed).toEqual(original);
  });

  it("multi_value default survives multiple round-trips without corruption", () => {
    let value: unknown = ["Sales", "Marketing", "Engineering"];
    for (let i = 0; i < 5; i++) {
      const text = formatDefaultValue(value);
      value = parseDefaultValue(text, "multi_value");
    }
    expect(value).toEqual(["Sales", "Marketing", "Engineering"]);
  });
});

describe("formatDefaultValue preserves other types", () => {
  it("null/undefined -> empty string", () => {
    expect(formatDefaultValue(null)).toBe("");
    expect(formatDefaultValue(undefined)).toBe("");
  });

  it("string -> string", () => {
    expect(formatDefaultValue("hello")).toBe("hello");
  });

  it("number -> string", () => {
    expect(formatDefaultValue(42)).toBe("42");
  });

  it("boolean -> string", () => {
    expect(formatDefaultValue(true)).toBe("true");
  });

  it("date_range object -> JSON string", () => {
    const dr = { from: "2026-01-01", to: "2026-12-31" };
    expect(formatDefaultValue(dr)).toBe(JSON.stringify(dr));
  });
});

describe("parseDefaultValue type handling", () => {
  it("empty string -> undefined for all types", () => {
    expect(parseDefaultValue("", "string")).toBeUndefined();
    expect(parseDefaultValue("  ", "number")).toBeUndefined();
    expect(parseDefaultValue("", "multi_value")).toBeUndefined();
  });

  it("number type parses numeric string", () => {
    expect(parseDefaultValue("42", "number")).toBe(42);
  });

  it("boolean type parses true/false", () => {
    expect(parseDefaultValue("true", "boolean")).toBe(true);
    expect(parseDefaultValue("false", "boolean")).toBe(false);
  });

  it("string type returns as-is", () => {
    expect(parseDefaultValue("hello", "string")).toBe("hello");
  });

  it("date_range type parses JSON", () => {
    const json = '{"from":"2026-01-01","to":"2026-12-31"}';
    expect(parseDefaultValue(json, "date_range")).toEqual({
      from: "2026-01-01",
      to: "2026-12-31",
    });
  });
});

describe("F-029-04: parameter name grammar mirrors the backend", () => {
  it("accepts @name with letters, digits, and underscores", () => {
    expect(PARAM_NAME_RE.test("@region")).toBe(true);
    expect(PARAM_NAME_RE.test("@region_code")).toBe(true);
    expect(PARAM_NAME_RE.test("@_x1")).toBe(true);
  });

  it("rejects the shapes the old startsWith('@') check let through", () => {
    expect(PARAM_NAME_RE.test("@1x")).toBe(false); // digit after @
    expect(PARAM_NAME_RE.test("@region-code")).toBe(false); // hyphen
    expect(PARAM_NAME_RE.test("@")).toBe(false);
    expect(PARAM_NAME_RE.test("region")).toBe(false); // no @
  });
});

describe("F-029-05: default value validation blocks silent corruption", () => {
  it("boolean accepts the backend token set, no longer forcing false", () => {
    expect(parseDefaultValue("yes", "boolean")).toBe(true);
    expect(parseDefaultValue("1", "boolean")).toBe(true);
    expect(parseDefaultValue("no", "boolean")).toBe(false);
    expect(parseDefaultValue("0", "boolean")).toBe(false);
  });

  it("flags an invalid number/boolean/date_range default", () => {
    expect(defaultValueError("hello", "number")).toBe(
      "parameters.defaultValueNumberError",
    );
    expect(defaultValueError("maybe", "boolean")).toBe(
      "parameters.defaultValueBooleanError",
    );
    expect(defaultValueError("{bad json", "date_range")).toBe(
      "parameters.defaultValueDateRangeError",
    );
    expect(defaultValueError('{"from":"2025-01-01"}', "date_range")).toBe(
      "parameters.defaultValueDateRangeError",
    );
  });

  it("accepts valid defaults and an empty default", () => {
    expect(defaultValueError("", "number")).toBeNull();
    expect(defaultValueError("42", "number")).toBeNull();
    expect(defaultValueError("yes", "boolean")).toBeNull();
    expect(
      defaultValueError('{"from":"2025-01-01","to":"2025-12-31"}', "date_range"),
    ).toBeNull();
  });
});

describe("L13-PERSONA-AT: typed parameter editor codec", () => {
  it("preserves structured arrays, including values containing commas", () => {
    const original = ["North, America", "EMEA", 7, true];
    const encoded = formatDefaultValue(original, "multi_value");
    expect(parseDefaultValue(encoded, "multi_value")).toEqual(original);
  });

  it("preserves only the date_range from/to contract", () => {
    const original = { from: "2026-01-01", to: "2026-12-31" };
    const encoded = formatDefaultValue(original, "date_range");
    expect(parseDefaultValue(encoded, "date_range")).toEqual(original);
    expect(defaultValueError('{"from":"2026-01-01","to":"2026-12-31","x":1}', "date_range"))
      .toBe("parameters.defaultValueDateRangeError");
  });

  it("keeps typed scalar parsing separate from structured values", () => {
    expect(parseDefaultValue("42.50", "number")).toBe(42.5);
    expect(parseDefaultValue("yes", "boolean")).toBe(true);
    expect(parseDefaultValue("001", "string")).toBe("001");
  });
});
