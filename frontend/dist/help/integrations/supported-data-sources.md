---
title: "Supported Data Sources"
audience: modeller
area: Integrations
updated: 2026-05-18
---

## What this covers

Tessallite connects to backend data sources to read raw data and write pre-aggregated summaries. This article lists every supported source, its driver, connection parameters, and permission requirements.

---

## Summary

| Data source | Identifier quoting | Driver | Auth method | Status |
|-------------|-------------------|--------|-------------|--------|
| PostgreSQL | `"double-quote"` | asyncpg (bundled) | Username / password | GA |
| Amazon Redshift | `"double-quote"` | asyncpg (bundled) | Username / password | GA |
| BigQuery | `` `backtick` `` | google-cloud-bigquery (bundled) | Service account JSON key | GA |
| Snowflake | `"double-quote"` | snowflake-connector-python (optional) | Username / password | GA |
| Hadoop / Spark | `` `backtick` `` | pyhive (bundled) | None, LDAP, or Kerberos | GA |
| SQL Server | `[bracket]` | aioodbc / pyodbc (optional) | Username / password | GA |

---

## PostgreSQL

Driver bundled. No additional installation required.

| Parameter | Value |
|-----------|-------|
| Host | Hostname or IP of the PostgreSQL server |
| Port | `5432` (default) |
| Database | PostgreSQL database name |
| User | PostgreSQL user with read access to source schemas |
| Password | PostgreSQL user password |

**Compatible managed services**: AWS RDS for PostgreSQL, Azure Database for PostgreSQL, Google Cloud SQL for PostgreSQL, Supabase, Neon.

**Permissions required**: `USAGE` on source schemas, `SELECT` on source tables. For aggregate writes: `CREATE TABLE`, `INSERT`, `DROP TABLE` on the target schema.

**Default schema**: `public`.

---

## Amazon Redshift

Uses the same asyncpg driver as PostgreSQL (Redshift is wire-compatible with PostgreSQL). No additional driver installation required.

| Parameter | Value |
|-----------|-------|
| Host | Redshift cluster endpoint |
| Port | `5439` (default) |
| Database | Redshift database name |
| User | Redshift user |
| Password | Redshift user password |

**Permissions required**: `SELECT` on source tables. For aggregate writes: `CREATE TABLE`, `INSERT`, `DROP TABLE` on the target schema.

**Default schema**: `public`.

---

## BigQuery

Tessallite connects to BigQuery via the google-cloud-bigquery Python client library. No JDBC JAR is needed for the Tessallite backend; the Simba JDBC driver is only needed if BI tools connect to Tessallite's JDBC gateway.

| Parameter | Value |
|-----------|-------|
| GCP project ID | Your Google Cloud project ID |
| Dataset name | BigQuery dataset containing source tables |
| Service account key | JSON key content pasted into the credentials field |

**Permissions required**: Service account must have `bigquery.jobs.create` and `bigquery.tables.getData` IAM roles. For aggregate writes, add `bigquery.tables.create`, `bigquery.tables.updateData`, and `bigquery.tables.delete`.

**Query dialect**: Standard SQL.

**Default dataset**: The dataset specified in the connection.

---

## Snowflake

Requires `snowflake-connector-python`. Install the optional dependency: `pip install tessallite-shared[snowflake]`.

| Parameter | Value |
|-----------|-------|
| Account | Snowflake account identifier (e.g. `xy12345.us-east-1.aws`) |
| Username | Snowflake user |
| Password | Snowflake user password |
| Database | Snowflake database name |
| Schema | Default schema (default: `PUBLIC`) |
| Warehouse | Compute warehouse for query execution |
| Role | Session role (optional) |

**Permissions required**: `USAGE` on the warehouse and database, `SELECT` on source tables. For aggregate writes: `CREATE TABLE`, `INSERT`, `DROP TABLE` on the target schema.

**Default schema**: `PUBLIC`.

---

## Hadoop / Spark Thrift Server

Hive Thrift driver (pyhive) is bundled with Tessallite.

| Parameter | Value |
|-----------|-------|
| Host | Hostname or IP of the Spark Thrift Server |
| Port | `10000` (default) |
| Database | Hive/Spark database name |
| Authentication | `NOSASL` (default) or `LDAP` |

**Tested with**: Apache Spark 3.x Thrift Server, Apache Hive 3.x.

**LDAP authentication**: Provide username and password when auth method is set to LDAP.

**Kerberos**: Requires additional configuration (keytab path, principal). See the system admin deployment guide.

**Databricks SQL Warehouse**: Not directly supported via this connector. Use the Databricks JDBC driver through the gateway's custom JDBC path.

**Default database**: `default`.

---

## SQL Server

Requires `aioodbc` (async wrapper over pyodbc) and an ODBC driver. Install the optional dependency: `pip install tessallite-shared[sqlserver]`.

The ODBC driver must also be installed on the Tessallite host. The default is `ODBC Driver 17 for SQL Server` from Microsoft.

### Installing the ODBC driver

**Debian / Ubuntu:**

```
curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add -
curl https://packages.microsoft.com/config/ubuntu/$(lsb_release -rs)/prod.list \
  > /etc/apt/sources.list.d/mssql-release.list
apt-get update && ACCEPT_EULA=Y apt-get install -y msodbcsql17
```

**Docker**: Add the above to your Dockerfile or use a base image that includes the driver.

### Connection parameters

| Parameter | Value |
|-----------|-------|
| Host | Hostname or IP of the SQL Server instance |
| Port | `1433` (default) |
| Database | SQL Server database name |
| Username | SQL Server login |
| Password | SQL Server login password |
| Schema | Default schema (default: `dbo`) |
| ODBC Driver | Driver name (default: `ODBC Driver 17 for SQL Server`) |
| Encrypt | Whether to encrypt the connection (optional) |
| Trust Server Certificate | Accept self-signed certificates (optional, non-production) |

**Compatible managed services**: Azure SQL Database, Azure SQL Managed Instance, AWS RDS for SQL Server, Google Cloud SQL for SQL Server.

**Permissions required**: `SELECT` on source tables in the connected database. For aggregate writes: `CREATE TABLE`, `INSERT`, `DROP TABLE`, and `ALTER` on the target schema.

**Identifier quoting**: SQL Server uses bracket quoting (`[schema].[table]`). Tessallite handles this automatically through the connector quoting layer.

**Default schema**: `dbo`.

---

## Choosing a source type

- **PostgreSQL and Redshift** use the same asyncpg driver. Redshift is identified separately so Tessallite can apply Redshift-specific SQL dialect rules.
- **BigQuery** requires a GCP service account. Tessallite communicates via the BigQuery client library, not JDBC.
- **Snowflake** and **SQL Server** are optional dependencies. Install them only if you connect to those sources.
- **Hadoop/Spark** is for environments running a Hive Thrift Server. For Databricks, consider using the JDBC gateway path.

---

## Related

- [JDBC Connection Guide](jdbc-connection-guide.md)
- [BI Tool Compatibility Matrix](bi-compatibility.md)
- [API Authentication](api-authentication.md)
- [Aggregates Not Building](../troubleshooting/aggregates-not-building.md)
- [Common Errors](../troubleshooting/common-errors.md)

---

← [Power BI Connection Guide](powerbi-connection-guide.md) | [Home](../index.md) | [API Authentication →](api-authentication.md)
