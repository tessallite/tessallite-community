import { describe, it, expect } from "vitest";
import type { Measure } from "../../../api/types";
import {
  RECORD_COUNT_ID,
  buildColumnMeasures,
  columnAlias,
  recordCountMeasure,
  type MeasureSel,
} from "./measureColumns";

// Minimal translate stub: echoes the key and interpolates {{name}}/{{agg}} so
// display-name assertions stay deterministic without loading the i18n bundle.
const t = (key: string, params?: Record<string, string>): string => {
  if (key === "pickerBar.recordCount") return "Record Count";
  if (key === "pivot.measureWithAgg" && params) {
    return `${params.name} (${params.agg})`;
  }
  if (key.startsWith("pivot.agg.")) return key.slice("pivot.agg.".length).toUpperCase();
  return key;
};

function measure(id: string, overrides: Partial<Measure> = {}): Measure {
  return {
    id,
    name: id,
    display_name: id,
    source_column_id: null,
    source_column_name: id,
    source_table_id: null,
    user_defined_attribute_id: null,
    user_defined_attribute_name: null,
    measure_type: "standard",
    expression: null,
    default_agg: "sum",
    data_type: "numeric",
    format: null,
    is_additive: true,
    redundant_partner: null,
    ...overrides,
  };
}

describe("columnAlias", () => {
  it("is deterministic and includes base, agg, and index", () => {
    expect(columnAlias("revenue", "SUM", 0)).toBe("revenue__sum__0");
    expect(columnAlias("revenue", "AVG", 2)).toBe("revenue__avg__2");
  });
});

describe("recordCountMeasure", () => {
  it("builds an additive integer COUNT measure with the localized name", () => {
    const rc = recordCountMeasure(t);
    expect(rc.id).toBe(RECORD_COUNT_ID);
    expect(rc.is_additive).toBe(true);
    expect(rc.default_agg).toBe("count");
    expect(rc.display_name).toBe("Record Count");
  });
});

describe("buildColumnMeasures", () => {
  const revenue = measure("revenue", { display_name: "Revenue" });

  it("assigns a unique alias to each duplicate (measure, agg) selection", () => {
    const selections: MeasureSel[] = [
      { measureId: "revenue", agg: "SUM" },
      { measureId: "revenue", agg: "AVG" },
      { measureId: "revenue", agg: "SUM" },
    ];
    const cols = buildColumnMeasures(selections, [revenue], t);
    const aliases = cols.map((c) => c._alias);
    expect(aliases).toEqual([
      "revenue__sum__0",
      "revenue__avg__1",
      "revenue__sum__2",
    ]);
    // ``name`` must equal the alias so downstream keying stays unique.
    expect(cols.map((c) => c.name)).toEqual(aliases);
    expect(new Set(aliases).size).toBe(3);
  });

  it("suffixes the display name with the aggregate label", () => {
    const cols = buildColumnMeasures([{ measureId: "revenue", agg: "AVG" }], [revenue], t);
    expect(cols[0].display_name).toBe("Revenue (AVG)");
    expect(cols[0]._agg).toBe("AVG");
    expect(cols[0]._baseName).toBe("revenue");
    expect(cols[0]._measureId).toBe("revenue");
  });

  it("expands the record-count selection into a COUNT column", () => {
    const rc = recordCountMeasure(t);
    const cols = buildColumnMeasures(
      [{ measureId: RECORD_COUNT_ID, agg: "COUNT" }],
      [rc],
      t,
    );
    expect(cols).toHaveLength(1);
    expect(cols[0]._recordCount).toBe(true);
    expect(cols[0]._agg).toBe("COUNT");
    // Record count keeps its own localized label (no agg suffix).
    expect(cols[0].display_name).toBe("Record Count");
  });

  it("falls back to the measure default_agg when the selection agg is empty", () => {
    const cols = buildColumnMeasures([{ measureId: "revenue", agg: "" }], [revenue], t);
    expect(cols[0]._agg).toBe("SUM");
  });

  it("skips selections whose measure is not present", () => {
    const cols = buildColumnMeasures([{ measureId: "missing", agg: "SUM" }], [revenue], t);
    expect(cols).toEqual([]);
  });
});
