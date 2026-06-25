import type React from "react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ConnField {
  key: string;
  label: string;
  type?: "text" | "password" | "number" | "select" | "file" | "checkbox";
  required?: boolean;
  options?: { value: string; label: string }[];
  defaultValue?: string;
  placeholder?: string;
  helperText?: string;
  group: "credentials" | "config";
  /** Only show when another field equals a specific value */
  showWhen?: { field: string; value: string };
}

// ---------------------------------------------------------------------------
// Connection field definitions per type
// ---------------------------------------------------------------------------

const PG_FIELDS: ConnField[] = [
  { key: "host", label: "conn.field.host", required: true, placeholder: "db.example.com", group: "credentials" },
  { key: "port", label: "conn.field.port", type: "number", defaultValue: "5432", required: true, group: "credentials" },
  { key: "database", label: "conn.field.database", placeholder: "your_database", required: true, group: "credentials" },
  { key: "username", label: "conn.field.username", required: true, group: "credentials" },
  { key: "password", label: "conn.field.password", type: "password", required: true, group: "credentials" },
  {
    key: "schema", label: "conn.field.defaultSchema", placeholder: "public", group: "config",
    helperText: "conn.postgresql.defaultSchemaHelper",
  },
  {
    key: "write_access", label: "conn.field.writeAccess", type: "checkbox",
    group: "config",
    helperText: "conn.postgresql.writeAccessHelper",
  },
];

const BQ_FIELDS: ConnField[] = [
  { key: "project_id", label: "conn.bigquery.projectId", required: true, placeholder: "my-gcp-project", group: "credentials" },
  {
    key: "service_account_json", label: "conn.bigquery.serviceAccountKey", type: "file", required: true,
    helperText: "conn.bigquery.serviceAccountHelper", group: "credentials",
  },
  {
    key: "write_access", label: "conn.field.writeAccess", type: "checkbox",
    group: "config",
    helperText: "conn.bigquery.writeAccessHelper",
  },
];

const HADOOP_SPARK_FIELDS: ConnField[] = [
  { key: "host", label: "conn.field.host", required: true, placeholder: "spark-thrift.example.com", group: "credentials" },
  { key: "port", label: "conn.field.port", type: "number", defaultValue: "10000", required: true, group: "credentials" },
  { key: "database", label: "conn.field.database", defaultValue: "default", required: true, group: "credentials" },
  {
    key: "auth_method", label: "conn.field.authentication", type: "select", required: true, defaultValue: "NOSASL",
    options: [
      { value: "NOSASL", label: "conn.spark.authNone" },
      { value: "LDAP", label: "conn.spark.authLdap" },
    ],
    group: "credentials",
  },
  { key: "username", label: "conn.field.username", required: true, group: "credentials", showWhen: { field: "auth_method", value: "LDAP" } },
  { key: "password", label: "conn.field.password", type: "password", required: true, group: "credentials", showWhen: { field: "auth_method", value: "LDAP" } },
  {
    key: "schema", label: "conn.field.defaultSchema", group: "config",
    helperText: "conn.spark.schemaHelper",
  },
  {
    key: "write_access", label: "conn.field.writeAccess", type: "checkbox",
    group: "config",
    helperText: "conn.spark.writeAccessHelper",
  },
];

const REDSHIFT_FIELDS: ConnField[] = [
  { key: "host", label: "conn.redshift.clusterEndpoint", required: true, placeholder: "cluster.abc123.us-east-1.redshift.amazonaws.com", group: "credentials" },
  { key: "port", label: "conn.field.port", type: "number", defaultValue: "5439", required: true, group: "credentials" },
  { key: "database", label: "conn.field.database", required: true, group: "credentials" },
  { key: "username", label: "conn.field.username", required: true, group: "credentials" },
  { key: "password", label: "conn.field.password", type: "password", required: true, group: "credentials" },
  {
    key: "schema", label: "conn.field.defaultSchema", placeholder: "public", group: "config",
    helperText: "conn.redshift.defaultSchemaHelper",
  },
  {
    key: "write_access", label: "conn.field.writeAccess", type: "checkbox",
    group: "config",
    helperText: "conn.redshift.writeAccessHelper",
  },
];

const SNOWFLAKE_FIELDS: ConnField[] = [
  { key: "account", label: "conn.snowflake.accountIdentifier", required: true, placeholder: "xy12345.us-east-1.aws", group: "credentials",
    helperText: "conn.snowflake.accountHelper",
  },
  { key: "username", label: "conn.field.username", required: true, group: "credentials" },
  { key: "password", label: "conn.field.password", type: "password", required: true, group: "credentials" },
  { key: "database", label: "conn.field.database", required: true, group: "credentials" },
  { key: "schema", label: "conn.field.defaultSchema", placeholder: "PUBLIC", group: "config",
    helperText: "conn.snowflake.defaultSchemaHelper",
  },
  { key: "warehouse", label: "conn.snowflake.computeWarehouse", placeholder: "COMPUTE_WH", group: "config",
    helperText: "conn.snowflake.warehouseHelper",
  },
  { key: "role", label: "conn.snowflake.role", placeholder: "PUBLIC", group: "config",
    helperText: "conn.snowflake.roleHelper",
  },
  {
    key: "write_access", label: "conn.field.writeAccess", type: "checkbox",
    group: "config",
    helperText: "conn.snowflake.writeAccessHelper",
  },
];

const SQLSERVER_FIELDS: ConnField[] = [
  { key: "host", label: "conn.field.host", required: true, placeholder: "sql-server.example.com", group: "credentials" },
  { key: "port", label: "conn.field.port", type: "number", defaultValue: "1433", required: true, group: "credentials" },
  { key: "database", label: "conn.field.database", required: true, group: "credentials" },
  { key: "username", label: "conn.field.username", required: true, group: "credentials" },
  { key: "password", label: "conn.field.password", type: "password", required: true, group: "credentials" },
  { key: "schema", label: "conn.field.defaultSchema", placeholder: "dbo", group: "config",
    helperText: "conn.sqlserver.defaultSchemaHelper",
  },
  { key: "driver", label: "conn.sqlserver.odbcDriver", placeholder: "ODBC Driver 17 for SQL Server", group: "config",
    helperText: "conn.sqlserver.odbcDriverHelper",
  },
  {
    key: "encrypt", label: "conn.sqlserver.encryptConnection", type: "checkbox",
    group: "config",
    helperText: "conn.sqlserver.encryptHelper",
  },
  {
    key: "trust_server_certificate", label: "conn.sqlserver.trustCertificate", type: "checkbox",
    group: "config",
    helperText: "conn.sqlserver.trustCertificateHelper",
  },
  {
    key: "write_access", label: "conn.field.writeAccess", type: "checkbox",
    group: "config",
    helperText: "conn.sqlserver.writeAccessHelper",
  },
];

export const CONN_FIELDS: Record<string, ConnField[]> = {
  postgresql: PG_FIELDS,
  bigquery: BQ_FIELDS,
  hadoop_spark: HADOOP_SPARK_FIELDS,
  redshift: REDSHIFT_FIELDS,
  snowflake: SNOWFLAKE_FIELDS,
  sqlserver: SQLSERVER_FIELDS,
  // Legacy alias so any old ``connection_type="jdbc"`` row still resolves
  // to the Spark/Hive field set (Phase C legacy fallback).
  jdbc: HADOOP_SPARK_FIELDS,
};

// ---------------------------------------------------------------------------
// Source / Target config field definitions
// ---------------------------------------------------------------------------

const HADOOP_SPARK_SOURCE_FIELDS: ConnField[] = [
  { key: "database", label: "conn.field.database", defaultValue: "default", group: "config" },
  { key: "table", label: "conn.field.table", required: true, group: "config" },
];

const SNOWFLAKE_SOURCE_FIELDS: ConnField[] = [
  { key: "schema", label: "conn.field.schema", defaultValue: "PUBLIC", required: true, group: "config" },
  { key: "table", label: "conn.field.table", required: true, group: "config" },
];

const REDSHIFT_SOURCE_FIELDS: ConnField[] = [
  { key: "schema", label: "conn.field.schema", defaultValue: "public", required: true, group: "config" },
  { key: "table", label: "conn.field.table", required: true, group: "config" },
];

const SQLSERVER_SOURCE_FIELDS: ConnField[] = [
  { key: "schema", label: "conn.field.schema", defaultValue: "dbo", required: true, group: "config" },
  { key: "table", label: "conn.field.table", required: true, group: "config" },
];

export const SOURCE_TARGET_FIELDS: Record<string, ConnField[]> = {
  bigquery: [
    { key: "dataset", label: "conn.field.dataset", required: true, group: "config" },
    { key: "table", label: "conn.field.table", required: true, group: "config" },
  ],
  postgresql: [
    { key: "schema", label: "conn.field.schema", defaultValue: "public", required: true, group: "config" },
    { key: "table", label: "conn.field.table", required: true, group: "config" },
  ],
  hadoop_spark: HADOOP_SPARK_SOURCE_FIELDS,
  redshift: REDSHIFT_SOURCE_FIELDS,
  snowflake: SNOWFLAKE_SOURCE_FIELDS,
  sqlserver: SQLSERVER_SOURCE_FIELDS,
  jdbc: HADOOP_SPARK_SOURCE_FIELDS, // legacy alias, Phase C
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export function buildJsonFromFields(
  fields: ConnField[],
  values: Record<string, string>,
  group: "credentials" | "config",
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const f of fields) {
    if (f.group !== group) continue;
    if (f.type === "checkbox") {
      // Always emit the boolean — even false — so toggling off the
      // checkbox actually clears the flag on the stored config rather
      // than silently keeping the old value.
      result[f.key] = (values[f.key] ?? f.defaultValue ?? "") === "true";
      continue;
    }
    const val = values[f.key] ?? f.defaultValue ?? "";
    if (!val) continue;
    if (f.key === "service_account_json") {
      try { result[f.key] = JSON.parse(val); } catch { result[f.key] = val; }
      continue;
    }
    result[f.key] = f.type === "number" ? Number(val) : val;
  }
  return result;
}

export function isFieldVisible(
  field: ConnField,
  values: Record<string, string>,
): boolean {
  if (!field.showWhen) return true;
  return (values[field.showWhen.field] ?? "") === field.showWhen.value;
}

export function getDefaultValues(fields: ConnField[]): Record<string, string> {
  const defaults: Record<string, string> = {};
  for (const f of fields) {
    if (f.defaultValue) defaults[f.key] = f.defaultValue;
  }
  return defaults;
}
