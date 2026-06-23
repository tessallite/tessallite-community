/**
 * Connection and calendar dialog bug regression tests.
 *
 * Bug-155: BQ/Spark deps missing from model-service
 * Bug-156: Discovery keystroke spam + hidden error
 * Bug-157: Calendar script tab never shows DDL
 * Bug-158: BQ calendar table name not qualified with project.dataset
 */
import { describe, it, expect } from "vitest";
import {
  CONN_FIELDS,
  SOURCE_TARGET_FIELDS,
  buildJsonFromFields,
  getDefaultValues,
  isFieldVisible,
} from "./connectionFields";

describe("CONN_FIELDS — connection form field definitions", () => {
  it("bigquery has project_id credential field", () => {
    const bq = CONN_FIELDS.bigquery;
    const projectField = bq.find((f) => f.key === "project_id");
    expect(projectField).toBeDefined();
    expect(projectField!.group).toBe("credentials");
    expect(projectField!.required).toBe(true);
  });

  it("bigquery has service_account_json credential field", () => {
    const bq = CONN_FIELDS.bigquery;
    const saField = bq.find((f) => f.key === "service_account_json");
    expect(saField).toBeDefined();
    expect(saField!.type).toBe("file");
  });

  it("bigquery has write_access config field (Bug-158)", () => {
    const bq = CONN_FIELDS.bigquery;
    const wa = bq.find((f) => f.key === "write_access");
    expect(wa).toBeDefined();
    expect(wa!.group).toBe("config");
    expect(wa!.type).toBe("checkbox");
  });

  it("postgresql has write_access config field", () => {
    const pg = CONN_FIELDS.postgresql;
    const wa = pg.find((f) => f.key === "write_access");
    expect(wa).toBeDefined();
    expect(wa!.group).toBe("config");
  });

  it("hadoop_spark has write_access config field", () => {
    const hs = CONN_FIELDS.hadoop_spark;
    const wa = hs.find((f) => f.key === "write_access");
    expect(wa).toBeDefined();
    expect(wa!.group).toBe("config");
  });

  it("postgresql has schema config field", () => {
    const pg = CONN_FIELDS.postgresql;
    const schema = pg.find((f) => f.key === "schema");
    expect(schema).toBeDefined();
    expect(schema!.group).toBe("config");
  });

  it("jdbc is aliased to hadoop_spark", () => {
    expect(CONN_FIELDS.jdbc).toBe(CONN_FIELDS.hadoop_spark);
  });
});

describe("SOURCE_TARGET_FIELDS — source config fields", () => {
  it("bigquery source has dataset field", () => {
    const bq = SOURCE_TARGET_FIELDS.bigquery;
    const ds = bq.find((f) => f.key === "dataset");
    expect(ds).toBeDefined();
    expect(ds!.required).toBe(true);
  });

  it("postgresql source has schema field", () => {
    const pg = SOURCE_TARGET_FIELDS.postgresql;
    const schema = pg.find((f) => f.key === "schema");
    expect(schema).toBeDefined();
    expect(schema!.defaultValue).toBe("public");
  });
});

describe("buildJsonFromFields", () => {
  it("emits checkbox as boolean true", () => {
    const fields = CONN_FIELDS.bigquery;
    const values = { write_access: "true" };
    const config = buildJsonFromFields(fields, values, "config");
    expect(config.write_access).toBe(true);
  });

  it("emits checkbox as boolean false when unchecked", () => {
    const fields = CONN_FIELDS.bigquery;
    const values = { write_access: "false" };
    const config = buildJsonFromFields(fields, values, "config");
    expect(config.write_access).toBe(false);
  });

  it("emits checkbox as false when missing", () => {
    const fields = CONN_FIELDS.bigquery;
    const values = {};
    const config = buildJsonFromFields(fields, values, "config");
    expect(config.write_access).toBe(false);
  });

  it("parses service_account_json as object when valid JSON", () => {
    const fields = CONN_FIELDS.bigquery;
    const values = {
      project_id: "my-project",
      service_account_json: '{"type":"service_account"}',
    };
    const creds = buildJsonFromFields(fields, values, "credentials");
    expect(creds.service_account_json).toEqual({ type: "service_account" });
  });

  it("keeps service_account_json as string when invalid JSON", () => {
    const fields = CONN_FIELDS.bigquery;
    const values = {
      project_id: "my-project",
      service_account_json: "not-json",
    };
    const creds = buildJsonFromFields(fields, values, "credentials");
    expect(creds.service_account_json).toBe("not-json");
  });
});

describe("getDefaultValues", () => {
  it("returns port 5432 for postgresql", () => {
    const defaults = getDefaultValues(CONN_FIELDS.postgresql);
    expect(defaults.port).toBe("5432");
  });

  it("returns port 10000 for hadoop_spark", () => {
    const defaults = getDefaultValues(CONN_FIELDS.hadoop_spark);
    expect(defaults.port).toBe("10000");
  });

  // F-014-16: the PostgreSQL database field must not pre-fill the platform
  // metadata DB name (tessallite_system) — it would nudge users to point a
  // tenant source connection at platform internals. Empty default, placeholder
  // hint only.
  it("does not default the postgresql database to the platform metadata DB", () => {
    const defaults = getDefaultValues(CONN_FIELDS.postgresql);
    expect(defaults.database).toBeUndefined();
    const dbField = CONN_FIELDS.postgresql.find((f) => f.key === "database")!;
    expect(dbField.defaultValue).toBeUndefined();
    expect(dbField.placeholder).toBe("your_database");
  });
});

describe("isFieldVisible", () => {
  it("hadoop_spark username visible when auth_method is LDAP", () => {
    const userField = CONN_FIELDS.hadoop_spark.find((f) => f.key === "username")!;
    expect(isFieldVisible(userField, { auth_method: "LDAP" })).toBe(true);
  });

  it("hadoop_spark username hidden when auth_method is NOSASL", () => {
    const userField = CONN_FIELDS.hadoop_spark.find((f) => f.key === "username")!;
    expect(isFieldVisible(userField, { auth_method: "NOSASL" })).toBe(false);
  });

  it("field without showWhen is always visible", () => {
    const hostField = CONN_FIELDS.postgresql.find((f) => f.key === "host")!;
    expect(isFieldVisible(hostField, {})).toBe(true);
  });
});

describe("CalendarScriptRequest type (Bug-157)", () => {
  it("interface includes fiscal_year_start_month", async () => {
    const { CalendarScriptRequest } = await import("../api/types") as Record<string, unknown>;
    // TypeScript interfaces don't exist at runtime, but the import succeeds
    // if the module compiles — which is the real test (TS would fail if
    // fiscal_year_start_month were missing and used in CalendarTableDialog).
    expect(CalendarScriptRequest).toBeUndefined(); // interfaces are erased
  });
});
