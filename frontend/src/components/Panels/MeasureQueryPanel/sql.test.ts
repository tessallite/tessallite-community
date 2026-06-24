import { describe, it, expect } from "vitest";
import type { Dimension, Measure, Model } from "../../../api/types";
import { buildPivotSql, validateScratchpadExpression } from "./sql";
import { columnAlias, type PivotColumnMeasure } from "./measureColumns";
import type { Slicer } from "./types";

function dim(name: string, overrides: Partial<Dimension> = {}): Dimension {
  return {
    id: name,
    name,
    display_name: name,
    source_column_id: null,
    source_column_name: null,
    source_table_id: null,
    source_table_alias: null,
    source_table_display_name: null,
    user_defined_attribute_id: null,
    user_defined_attribute_name: null,
    hierarchy: null,
    is_time_dim: false,
    time_grain: null,
    redundant_partner: null,
    ...overrides,
  };
}

function measure(
  name: string,
  overrides: Partial<Measure> = {},
): Measure {
  return {
    id: name,
    name,
    display_name: name,
    model_id: "m",
    source_table_id: null,
    default_agg: "sum",
    format_string: null,
    variants: [],
    ...overrides,
  } as Measure;
}

/**
 * Build a single pivot column measure for the new buildPivotSql signature.
 * Mirrors buildColumnMeasures: name === alias, _agg upper-case, _baseName the
 * source column. ``idx`` keeps aliases unique across duplicate (measure, agg).
 */
function col(
  baseName: string,
  agg = "SUM",
  idx = 0,
  overrides: Partial<PivotColumnMeasure> = {},
): PivotColumnMeasure {
  const A = agg.toUpperCase();
  const alias = columnAlias(baseName, A, idx);
  return {
    ...measure(baseName),
    name: alias,
    _alias: alias,
    _agg: A,
    _baseName: baseName,
    _measureId: baseName,
    ...overrides,
  } as PivotColumnMeasure;
}

const model = { slug: "orders" } as Model;

describe("buildPivotSql — slicer operators", () => {
  it("generates date range slicer with BETWEEN", () => {
    const d = dim("order_date", { is_time_dim: true });
    const slicers: Slicer[] = [
      { dimensionId: "order_date", op: "between", values: ["2024-01-01", "2024-12-31"] },
    ];
    const sql = buildPivotSql(model, [col("revenue")], [d], [], slicers, [d]);
    expect(sql).toContain("BETWEEN '2024-01-01' AND '2024-12-31'");
  });

  it("generates >= operator", () => {
    const d = dim("amount");
    const slicers: Slicer[] = [
      { dimensionId: "amount", op: "gte", values: ["100"] },
    ];
    const sql = buildPivotSql(model, [col("revenue")], [d], [], slicers, [d]);
    expect(sql).toContain('"amount" >= \'100\'');
  });

  it("generates <= operator", () => {
    const d = dim("amount");
    const slicers: Slicer[] = [
      { dimensionId: "amount", op: "lte", values: ["500"] },
    ];
    const sql = buildPivotSql(model, [col("revenue")], [d], [], slicers, [d]);
    expect(sql).toContain('"amount" <= \'500\'');
  });

  it("generates > operator", () => {
    const d = dim("price");
    const slicers: Slicer[] = [
      { dimensionId: "price", op: "gt", values: ["50"] },
    ];
    const sql = buildPivotSql(model, [col("revenue")], [d], [], slicers, [d]);
    expect(sql).toContain('"price" > \'50\'');
  });

  it("generates < operator", () => {
    const d = dim("price");
    const slicers: Slicer[] = [
      { dimensionId: "price", op: "lt", values: ["200"] },
    ];
    const sql = buildPivotSql(model, [col("revenue")], [d], [], slicers, [d]);
    expect(sql).toContain('"price" < \'200\'');
  });

  it("generates <> (not equal) operator", () => {
    const d = dim("region");
    const slicers: Slicer[] = [
      { dimensionId: "region", op: "ne", values: ["APAC"] },
    ];
    const sql = buildPivotSql(model, [col("revenue")], [d], [], slicers, [d]);
    expect(sql).toContain('"region" <> \'APAC\'');
  });

  it("combines multiple range operators", () => {
    const d = dim("amount");
    const slicers: Slicer[] = [
      { dimensionId: "amount", op: "gte", values: ["100"] },
      { dimensionId: "amount", op: "lte", values: ["500"] },
    ];
    const sql = buildPivotSql(model, [col("revenue")], [d], [], slicers, [d, d]);
    expect(sql).toContain('>= \'100\'');
    expect(sql).toContain('<= \'500\'');
    expect(sql).toContain(" AND ");
  });

  it("skips operator with empty value", () => {
    const d = dim("amount");
    const slicers: Slicer[] = [
      { dimensionId: "amount", op: "gte", values: [] },
    ];
    const sql = buildPivotSql(model, [col("revenue")], [d], [], slicers, [d]);
    expect(sql).not.toContain("WHERE");
  });
});

describe("buildPivotSql — measure aggregation (Bug-150)", () => {
  it("wraps measure with SUM", () => {
    const sql = buildPivotSql(model, [col("base_amount", "SUM")], [], [], [], []);
    expect(sql).toBe(
      'SELECT SUM("base_amount") AS "base_amount__sum__0" FROM "orders"',
    );
  });

  it("wraps measure with AVG", () => {
    const sql = buildPivotSql(model, [col("unit_price", "AVG")], [], [], [], []);
    expect(sql).toContain('AVG("unit_price")');
  });

  it("wraps measure with MIN", () => {
    const sql = buildPivotSql(model, [col("amount", "MIN")], [], [], [], []);
    expect(sql).toContain('MIN("amount")');
  });

  it("wraps measure with MAX", () => {
    const sql = buildPivotSql(model, [col("amount", "MAX")], [], [], [], []);
    expect(sql).toContain('MAX("amount")');
  });

  it("wraps measure with COUNT", () => {
    const sql = buildPivotSql(model, [col("order_id", "COUNT")], [], [], [], []);
    expect(sql).toContain('COUNT("order_id")');
  });

  it("wraps count_distinct as COUNT(DISTINCT ...)", () => {
    const sql = buildPivotSql(
      model,
      [col("customer_id", "COUNT_DISTINCT")],
      [],
      [],
      [],
      [],
    );
    expect(sql).toContain('COUNT(DISTINCT "customer_id")');
  });

  it("falls back to SUM when column _agg is empty", () => {
    const sql = buildPivotSql(
      model,
      [col("revenue", "SUM", 0, { _agg: "" })],
      [],
      [],
      [],
      [],
    );
    expect(sql).toContain('SUM("revenue")');
  });

  it("emits COUNT(*) for the record-count column", () => {
    const rc = col("__record_count__", "COUNT", 0, { _recordCount: true });
    const sql = buildPivotSql(model, [rc], [], [], [], []);
    expect(sql).toBe(
      'SELECT COUNT(*) AS "__record_count____count__0" FROM "orders"',
    );
  });

  it("assigns unique aliases when the same measure repeats under different aggs", () => {
    const cols = [
      col("revenue", "SUM", 0),
      col("revenue", "AVG", 1),
      col("revenue", "MAX", 2),
    ];
    const sql = buildPivotSql(model, cols, [], [], [], []);
    expect(sql).toContain('SUM("revenue") AS "revenue__sum__0"');
    expect(sql).toContain('AVG("revenue") AS "revenue__avg__1"');
    expect(sql).toContain('MAX("revenue") AS "revenue__max__2"');
  });

  it("never emits bare column name for a measure (regression guard)", () => {
    const sql = buildPivotSql(model, [col("base_amount", "SUM")], [], [], [], []);
    expect(sql).not.toMatch(/SELECT\s+"base_amount"\s+FROM/);
  });

  it("includes aggregate in SELECT alongside dimensions and GROUP BY", () => {
    const d = dim("region");
    const sql = buildPivotSql(model, [col("revenue", "SUM")], [d], [], [], []);
    expect(sql).toBe(
      'SELECT "region", SUM("revenue") AS "revenue__sum__0" FROM "orders" GROUP BY "region"',
    );
  });

  it("produces no GROUP BY when no dimensions are selected", () => {
    const sql = buildPivotSql(model, [col("revenue", "SUM")], [], [], [], []);
    expect(sql).not.toContain("GROUP BY");
    expect(sql).toContain('SUM("revenue")');
  });

  it("de-duplicates dimensions across rows and columns", () => {
    const d = dim("country");
    const sql = buildPivotSql(model, [col("revenue", "SUM")], [d], [d], [], []);
    const matches = sql.match(/"country"/g);
    expect(matches).toHaveLength(2); // once in SELECT, once in GROUP BY
  });

  it("handles multiple row and column dimensions with aggregate", () => {
    const d1 = dim("region");
    const d2 = dim("year");
    const d3 = dim("channel");
    const sql = buildPivotSql(
      model,
      [col("revenue", "SUM")],
      [d1, d2],
      [d3],
      [],
      [],
    );
    expect(sql).toContain('"region"');
    expect(sql).toContain('"year"');
    expect(sql).toContain('"channel"');
    expect(sql).toContain('SUM("revenue")');
    expect(sql).toContain('GROUP BY "region", "year", "channel"');
  });

  it("combines aggregate with WHERE slicers", () => {
    const d = dim("region");
    const slicers: Slicer[] = [
      { dimensionId: "region", op: "eq", values: ["EMEA"] },
    ];
    const sql = buildPivotSql(model, [col("revenue")], [d], [], slicers, [d]);
    expect(sql).toContain('SUM("revenue")');
    expect(sql).toContain("WHERE");
    expect(sql).toContain("'EMEA'");
    expect(sql).toContain('GROUP BY "region"');
  });
});

describe("validateScratchpadExpression (Bug-5315)", () => {
  it("allows a valid arithmetic expression", () => {
    expect(validateScratchpadExpression("SUM(a) / COUNT(b)")).toBeNull();
  });

  it("allows CASE expressions", () => {
    expect(
      validateScratchpadExpression("CASE WHEN x > 0 THEN x ELSE 0 END"),
    ).toBeNull();
  });

  it("blocks semicolons", () => {
    const err = validateScratchpadExpression("SUM(a); DROP TABLE t");
    expect(err).toBeTruthy();
    expect(err).toContain(";");
  });

  it("blocks DDL keywords (DROP)", () => {
    const err = validateScratchpadExpression("DROP TABLE orders");
    expect(err).toBeTruthy();
    expect(err).toContain("disallowed SQL keywords");
  });

  it("blocks INSERT keyword", () => {
    expect(validateScratchpadExpression("INSERT INTO t VALUES(1)")).toBeTruthy();
  });

  it("blocks DELETE keyword", () => {
    expect(validateScratchpadExpression("DELETE FROM t")).toBeTruthy();
  });

  it("rejects empty expression", () => {
    expect(validateScratchpadExpression("")).toBeTruthy();
    expect(validateScratchpadExpression("   ")).toBeTruthy();
  });

  it("rejects unbalanced parentheses", () => {
    expect(validateScratchpadExpression("SUM(a")).toBeTruthy();
    expect(validateScratchpadExpression("SUM(a))")).toBeTruthy();
  });
});
