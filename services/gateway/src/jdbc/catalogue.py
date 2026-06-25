"""
In-memory SQLite catalogue emulating PostgreSQL pg_catalog and
information_schema for the JDBC gateway.

Replaces pattern-matched metadata handlers with a real SQL engine.
Any client SQL that references system catalogues is executed against
SQLite; user queries fall through to the query-router.

OID layout:
  Public schema  : 2200
  Info schema    : 2201
  Tables         : 16384 + index
  Index objects  : table_oid + 100000
  PK constraints : table_oid + 200000
  FK constraints : table_oid + 300000 + ordinal
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timezone

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)


class CatalogueQueryError(Exception):
    """Raised when a genuinely catalogue-shaped query fails in the SQLite engine.

    F-001-03: lets the gateway emit an honest ErrorResponse instead of a
    fabricated empty success result.
    """


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp for the SQLite-registered now()/current_timestamp()."""
    return datetime.now(timezone.utc).isoformat(sep=" ", timespec="seconds")

PUBLIC_SCHEMA_OID = 2200
_INFO_SCHEMA_OID = 2201
_TABLE_OID_BASE = 16384
_VERSION_STRING = "PostgreSQL 15.0 (Tessallite Gateway)"

# -----------------------------------------------------------------------
# SQLite authorizer — strict read-only allowlist for the query connection
# -----------------------------------------------------------------------
# sqlite3 action codes used by the authorizer callback:
_SQLITE_SELECT = 21
_SQLITE_READ = 20
_SQLITE_FUNCTION = 31
_SQLITE_PRAGMA = 19

_SQLITE_OK = 0
_SQLITE_DENY = 1

# Actions we explicitly permit on the catalogue query connection.
_ALLOWED_ACTIONS: frozenset[int] = frozenset({
    _SQLITE_SELECT,
    _SQLITE_READ,
    _SQLITE_FUNCTION,
})


def _catalogue_authorizer(action: int, arg1, arg2, db_name, trigger) -> int:
    """Authorizer callback for the read-only catalogue connection.

    Permits only SELECT, READ, FUNCTION, and read-only PRAGMA operations.
    Everything else (ATTACH, DETACH, INSERT, UPDATE, DELETE, CREATE, DROP,
    ALTER, REINDEX, etc.) is denied.
    """
    if action in _ALLOWED_ACTIONS:
        return _SQLITE_OK
    # Allow read-only PRAGMAs (queries with no value argument).
    # arg1 is the pragma name, arg2 is the value (None for reads).
    if action == _SQLITE_PRAGMA and arg2 is None:
        return _SQLITE_OK
    return _SQLITE_DENY

# -----------------------------------------------------------------------
# Type OID constants and mappings
# -----------------------------------------------------------------------
_OID_BOOL = 16
_OID_INT2 = 21
_OID_INT4 = 23
_OID_INT8 = 20
_OID_OID = 26
_OID_TEXT = 25
_OID_VARCHAR = 1043
_OID_FLOAT4 = 700
_OID_FLOAT8 = 701
_OID_NUMERIC = 1700
_OID_DATE = 1082
_OID_TIMESTAMP = 1114
_OID_TIMESTAMPTZ = 1184

_DATA_TYPE_TO_OID: dict[str, int] = {
    "text": _OID_TEXT, "varchar": _OID_VARCHAR, "string": _OID_TEXT,
    "integer": _OID_INT4, "int": _OID_INT4, "int4": _OID_INT4,
    "bigint": _OID_INT8, "int8": _OID_INT8,
    "float": _OID_FLOAT8, "float4": _OID_FLOAT4, "float8": _OID_FLOAT8,
    "double": _OID_FLOAT8,
    "numeric": _OID_NUMERIC, "decimal": _OID_NUMERIC,
    "boolean": _OID_BOOL, "bool": _OID_BOOL,
    "date": _OID_DATE,
    "timestamp": _OID_TIMESTAMP, "timestamptz": _OID_TIMESTAMPTZ,
}

_PG_TYPES: list[tuple[int, str, str, int]] = [
    (16,   "bool",        "b", 1),
    (20,   "int8",        "b", 8),
    (23,   "int4",        "b", 4),
    (25,   "text",        "b", -1),
    (700,  "float4",      "b", 4),
    (701,  "float8",      "b", 8),
    (1043, "varchar",     "b", -1),
    (1082, "date",        "b", 4),
    (1114, "timestamp",   "b", 8),
    (1184, "timestamptz", "b", 8),
    (1700, "numeric",     "b", -1),
]

_TYPE_CATEGORIES: dict[str, str] = {
    "bool": "B",
    "int8": "N", "int4": "N", "float4": "N", "float8": "N", "numeric": "N",
    "text": "S", "varchar": "S",
    "date": "D", "timestamp": "D", "timestamptz": "D",
}

_TYPE_OID_TO_NAME: dict[int, str] = {oid: name for oid, name, _, _ in _PG_TYPES}


def _base_data_type(data_type: str | None) -> str:
    return (data_type or "text").lower().split("(", 1)[0].strip()


def _type_oid(data_type: str | None) -> int:
    return _DATA_TYPE_TO_OID.get(_base_data_type(data_type), _OID_TEXT)


def _numeric_metadata(data_type: str | None) -> tuple[str | None, str | None]:
    base_type = _base_data_type(data_type)
    if base_type not in {
        "integer", "int", "int4", "bigint", "int8",
        "float", "float4", "float8", "double", "numeric", "decimal",
    }:
        return None, None
    if base_type in {"integer", "int", "int4"}:
        return "32", "0"
    if base_type in {"bigint", "int8"}:
        return "64", "0"
    if base_type == "float4":
        return "24", None
    if base_type in {"float", "float8", "double"}:
        return "53", None
    match = re.search(r"\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)", data_type or "")
    if match:
        return match.group(1), match.group(2) or "0"
    return "38", None


def _build_trust_footer(trust: dict | None) -> str:
    if not trust:
        return ""
    parts: list[str] = []
    last = trust.get("last_refreshed_at")
    if last:
        cleaned = str(last).split(".")[0].replace("T", " ")
        parts.append(f"last refreshed {cleaned}")
    src = trust.get("source_system")
    if src:
        parts.append(f"source: {src}")
    owner = trust.get("owner")
    if owner:
        parts.append(f"owner: {owner}")
    return f"({', '.join(parts)})" if parts else ""


# -----------------------------------------------------------------------
# Detect whether SQL references catalogue objects
# -----------------------------------------------------------------------
_CATALOGUE_RE = re.compile(
    r"""
    \bpg_catalog\b | \bpg_namespace\b | \bpg_class\b | \bpg_attribute\b
    | \bpg_type\b | \bpg_proc\b | \bpg_database\b | \bpg_settings\b
    | \bpg_roles\b | \bpg_user\b | \bpg_tablespace\b | \bpg_index\b
    | \bpg_constraint\b | \bpg_description\b | \bpg_attrdef\b
    | \bpg_am\b | \bpg_amop\b | \bpg_amproc\b | \bpg_auth_members\b
    | \bpg_cast\b | \bpg_collation\b | \bpg_conversion\b
    | \bpg_default_acl\b | \bpg_depend\b | \bpg_enum\b
    | \bpg_event_trigger\b | \bpg_extension\b
    | \bpg_foreign_data_wrapper\b | \bpg_foreign_server\b
    | \bpg_foreign_table\b | \bpg_inherits\b | \bpg_language\b
    | \bpg_largeobject\b | \bpg_matviews\b | \bpg_opclass\b
    | \bpg_operator\b | \bpg_opfamily\b | \bpg_policy\b
    | \bpg_publication\b | \bpg_range\b | \bpg_rewrite\b
    | \bpg_seclabel\b | \bpg_sequence\b | \bpg_shdescription\b
    | \bpg_shseclabel\b | \bpg_statistic\b | \bpg_statistic_ext\b
    | \bpg_subscription\b | \bpg_transform\b | \bpg_trigger\b
    | \bpg_ts_config\b | \bpg_ts_dict\b | \bpg_ts_parser\b
    | \bpg_ts_template\b | \bpg_user_mapping\b | \bpg_views\b
    | \bpg_tables\b | \bpg_get_keywords\b
    | \binformation_schema\b
    | \binfo\.model_freshness\b | \binfo\.model_lineage\b
    | \binfo\.model_owners\b
    | \bversion\s*\(\s*\)
    | \bcurrent_schema\s*\(\s*\)
    | \bsession_user\b
    | \bcurrent_setting\s*\(
    """,
    re.IGNORECASE | re.VERBOSE,
)

# F-001-03: catalogue object / function names used by the AST router to
# confirm a query genuinely references the catalogue (not a model relation
# whose literal merely mentions a token). Schemas: pg_catalog / information_schema / info.
_CATALOGUE_SCHEMAS: frozenset[str] = frozenset({
    "pg_catalog", "information_schema", "info",
})
_CATALOGUE_TABLE_PREFIXES: tuple[str, ...] = ("pg_",)
_CATALOGUE_TABLE_NAMES: frozenset[str] = frozenset({
    "model_freshness", "model_lineage", "model_owners",
})

# -----------------------------------------------------------------------
# SQL transformation: PG SQL → SQLite-compatible SQL
# -----------------------------------------------------------------------
_PG_CATALOG_DOT_RE = re.compile(r"\bpg_catalog\.", re.IGNORECASE)
_INFO_SCHEMA_DOT_RE = re.compile(
    r"\binformation_schema\.(\w+)", re.IGNORECASE,
)
_INFO_DOT_RE = re.compile(r"\binfo\.(\w+)", re.IGNORECASE)
_REGTYPE_CAST_RE = re.compile(r"::regtype", re.IGNORECASE)
_REGCLASS_CAST_RE = re.compile(r"::regclass", re.IGNORECASE)
_REGPROC_CAST_RE = re.compile(r"::regproc", re.IGNORECASE)
_TEXT_CAST_RE = re.compile(r"::text", re.IGNORECASE)
_INT_CAST_RE = re.compile(r"::(?:int4|int8|integer|bigint|int|smallint|oid)", re.IGNORECASE)
_NAME_CAST_RE = re.compile(r"::name", re.IGNORECASE)
_ANY_CAST_RE = re.compile(r"::(?:pg_catalog\.)?(?:\"\w+\"|\w+)(?:\[\])?", re.IGNORECASE)
# session_user is a PG keyword that SQLite doesn't recognise; rewrite
# to a registered function call.
_SESSION_USER_RE = re.compile(r"\bsession_user\b", re.IGNORECASE)
# current_user is also a PG keyword
_CURRENT_USER_RE = re.compile(r"\bcurrent_user\b", re.IGNORECASE)
# Bug-5183: PostgreSQL ILIKE (case-insensitive LIKE) has no SQLite keyword.
# SQLite's bare LIKE is already case-insensitive for ASCII, which covers every
# catalogue identifier (schema/table/column names are ASCII), so ILIKE → LIKE
# is the semantically faithful rewrite for the information_schema query path.
_ILIKE_RE = re.compile(r"\bILIKE\b", re.IGNORECASE)
# Quote-as-ident: "pg_catalog"."pg_class" → pg_class
_QUOTED_PG_CATALOG_RE = re.compile(
    r'"pg_catalog"\s*\.\s*"(\w+)"', re.IGNORECASE,
)
# Array constructors: ARRAY[x, y] → 'x,y'
_ARRAY_CONSTRUCTOR_RE = re.compile(
    r"ARRAY\[([^\]]*)\]", re.IGNORECASE,
)
# ANY(ARRAY[...]) → IN (...)
_ANY_ARRAY_RE = re.compile(
    r"=\s*ANY\s*\(\s*ARRAY\s*\[([^\]]*)\]\s*\)", re.IGNORECASE,
)
# <> ALL ('{a,b,...}'::text[]) → NOT IN ('a','b',...)
_NEQ_ALL_PG_ARRAY_RE = re.compile(
    r"<>\s*ALL\s*\(\s*'\{([^}]*)\}'(?:::[^\)]+)?\s*\)", re.IGNORECASE,
)
# pg_get_expr with wrong number of args: already handled by registered
# function; we just need to ensure it passes through.


def _transform_sql(sql: str) -> str:
    """Rewrite PostgreSQL system catalogue SQL into SQLite-compatible SQL."""
    # Strip empty set patterns — we still execute, but WHERE 1<>1 is fine
    # in SQLite and will return zero rows.

    # "pg_catalog"."pg_class" → pg_class
    sql = _QUOTED_PG_CATALOG_RE.sub(r"\1", sql)
    # pg_catalog.X → X
    sql = _PG_CATALOG_DOT_RE.sub("", sql)
    # information_schema.X → information_schema_X
    sql = _INFO_SCHEMA_DOT_RE.sub(r"information_schema_\1", sql)
    # info.X → info_X
    sql = _INFO_DOT_RE.sub(r"info_\1", sql)
    # ANY(ARRAY[...]) → IN (...)
    sql = _ANY_ARRAY_RE.sub(r"IN (\1)", sql)
    # <> ALL ('{a,b,...}'::text[]) → NOT IN ('a','b',...)
    def _neq_all_rewrite(m: re.Match) -> str:
        items = m.group(1).split(",")
        quoted = ", ".join(f"'{item.strip()}'" for item in items)
        return f"NOT IN ({quoted})"
    sql = _NEQ_ALL_PG_ARRAY_RE.sub(_neq_all_rewrite, sql)
    # ARRAY[x, y] → 'x,y'
    sql = _ARRAY_CONSTRUCTOR_RE.sub(
        lambda m: "'" + m.group(1).replace("'", "") + "'", sql,
    )
    # Strip type casts
    sql = _REGTYPE_CAST_RE.sub("", sql)
    sql = _REGCLASS_CAST_RE.sub("", sql)
    sql = _REGPROC_CAST_RE.sub("", sql)
    sql = _TEXT_CAST_RE.sub("", sql)
    sql = _INT_CAST_RE.sub("", sql)
    sql = _NAME_CAST_RE.sub("", sql)
    sql = _ANY_CAST_RE.sub("", sql)
    # Bug-5183: ILIKE → LIKE (SQLite LIKE is case-insensitive for ASCII).
    sql = _ILIKE_RE.sub("LIKE", sql)
    # PG keywords that SQLite doesn't support as bare identifiers
    sql = _SESSION_USER_RE.sub("session_user_fn()", sql)
    sql = _CURRENT_USER_RE.sub("session_user_fn()", sql)
    # pg_get_keywords() is a table-valued function in PG; we have a table
    sql = re.sub(r"\bpg_get_keywords\s*\(\s*\)", "pg_get_keywords", sql, flags=re.IGNORECASE)
    return sql


# -----------------------------------------------------------------------
# Known column-name → OID map for RowDescription type hints
# -----------------------------------------------------------------------
_COLUMN_OID_HINTS: dict[str, int] = {
    "oid": _OID_INT8,
    "attrelid": _OID_INT8,
    "atttypid": _OID_INT4,
    "attstattarget": _OID_INT4,
    "attlen": _OID_INT4,
    "attnum": _OID_INT4,
    "attndims": _OID_INT4,
    "attcacheoff": _OID_INT4,
    "atttypmod": _OID_INT4,
    "attbyval": _OID_BOOL,
    "attnotnull": _OID_BOOL,
    "atthasdef": _OID_BOOL,
    "atthasmissing": _OID_BOOL,
    "attisdropped": _OID_BOOL,
    "attislocal": _OID_BOOL,
    "attinhcount": _OID_INT4,
    "attcollation": _OID_INT8,
    "relnamespace": _OID_INT8,
    "reltype": _OID_INT8,
    "reloftype": _OID_INT8,
    "relowner": _OID_INT8,
    "relam": _OID_INT8,
    "relpages": _OID_INT8,
    "reltuples": _OID_FLOAT4,
    "relhasindex": _OID_BOOL,
    "relisshared": _OID_BOOL,
    "relnatts": _OID_INT4,
    "relchecks": _OID_INT4,
    "relhasrules": _OID_BOOL,
    "relhastriggers": _OID_BOOL,
    "relhassubclass": _OID_BOOL,
    "relrowsecurity": _OID_BOOL,
    "relforcerowsecurity": _OID_BOOL,
    "relispopulated": _OID_BOOL,
    "relispartition": _OID_BOOL,
    "relfrozenxid": _OID_INT8,
    "relminmxid": _OID_INT8,
    "typnamespace": _OID_INT8,
    "typowner": _OID_INT8,
    "typelem": _OID_INT8,
    "typrelid": _OID_INT8,
    "typlen": _OID_INT4,
    "typbyval": _OID_BOOL,
    "typisdefined": _OID_BOOL,
    "typbasetype": _OID_INT8,
    "typtypmod": _OID_INT4,
    "typnotnull": _OID_BOOL,
    "nspowner": _OID_INT8,
    "ordinal_position": _OID_INT4,
    "numeric_precision": _OID_INT4,
    "numeric_scale": _OID_INT4,
    "pronamespace": _OID_INT8,
    "proowner": _OID_INT8,
    "pronargs": _OID_INT4,
    "prorettype": _OID_INT8,
    "proisagg": _OID_BOOL,
    "datdba": _OID_INT8,
    "encoding": _OID_INT4,
    "datistemplate": _OID_BOOL,
    "datallowconn": _OID_BOOL,
    "datconnlimit": _OID_INT4,
    "datlastsysoid": _OID_INT8,
    "datfrozenxid": _OID_INT8,
    "datminmxid": _OID_INT8,
    "dattablespace": _OID_INT8,
    "sourceline": _OID_INT4,
    "pending_restart": _OID_BOOL,
    "rolsuper": _OID_BOOL,
    "rolinherit": _OID_BOOL,
    "rolcreaterole": _OID_BOOL,
    "rolcreatedb": _OID_BOOL,
    "rolcanlogin": _OID_BOOL,
    "rolreplication": _OID_BOOL,
    "rolconnlimit": _OID_INT4,
    "rolbypassrls": _OID_BOOL,
    "spcowner": _OID_INT8,
    "spcmaxbytes": _OID_INT8,
    "indexrelid": _OID_INT8,
    "indrelid": _OID_INT8,
    "indnatts": _OID_INT4,
    "indisunique": _OID_BOOL,
    "indisprimary": _OID_BOOL,
    "conrelid": _OID_INT8,
    "confrelid": _OID_INT8,
    "objid": _OID_INT8,
}


class _StringAgg:
    """SQLite aggregate implementing PostgreSQL string_agg(value, sep)."""

    def __init__(self) -> None:
        self._values: list[str] = []
        self._sep = ","

    def step(self, value: str | None, sep: str | None) -> None:
        self._sep = sep or ","
        if value is not None:
            self._values.append(str(value))

    def finalize(self) -> str | None:
        return self._sep.join(self._values) if self._values else None


class CatalogueDB:
    """In-memory SQLite database holding PG system catalogue tables."""

    def __init__(
        self,
        model_names: list[str],
        table_columns: dict[str, list[dict]],
        table_descriptions: dict[str, str] | None = None,
        table_trust_meta: dict[str, dict] | None = None,
        table_model_id: dict[str, str] | None = None,
        table_foreign_keys: dict[str, list[dict[str, str]]] | None = None,
        table_row_estimates: dict[str, int | float | None] | None = None,
        looker_enabled: bool = True,
        looker_relations: set[str] | None = None,
        tenant_slug: str = "tessallite",
        table_project_slug: dict[str, str] | None = None,
    ) -> None:
        # Filter out Looker relations when disabled
        if not looker_enabled and looker_relations:
            model_names = [n for n in model_names if n not in looker_relations]
            table_columns = {
                k: v for k, v in table_columns.items() if k not in looker_relations
            }
            table_descriptions = {
                k: v for k, v in (table_descriptions or {}).items()
                if k not in looker_relations
            }
            table_trust_meta = {
                k: v for k, v in (table_trust_meta or {}).items()
                if k not in looker_relations
            }
            table_model_id = {
                k: v for k, v in (table_model_id or {}).items()
                if k not in looker_relations
            }
            table_foreign_keys = {
                k: v for k, v in (table_foreign_keys or {}).items()
                if k not in looker_relations
            }
            table_row_estimates = {
                k: v for k, v in (table_row_estimates or {}).items()
                if k not in looker_relations
            }

        self._model_names = model_names
        self._table_columns = table_columns
        self._table_descriptions = table_descriptions or {}
        self._table_trust_meta = table_trust_meta or {}
        self._table_model_id = table_model_id or {}
        self._table_foreign_keys = table_foreign_keys or {}
        self._table_row_estimates = table_row_estimates or {}
        self._tenant_slug = tenant_slug or "tessallite"
        self._table_project_slug = table_project_slug or {}

        # Build project-slug → namespace OID mapping. Each unique project
        # slug gets its own pg_namespace entry (OIDs starting at 2202).
        # Tables without a project slug fall back to "public" (OID 2200).
        unique_slugs = sorted(set(self._table_project_slug.values()))
        self._project_schema_oid: dict[str, int] = {}
        for i, slug in enumerate(unique_slugs):
            self._project_schema_oid[slug] = 2202 + i

        # Build the catalogue in a named in-memory database so we can
        # re-open it read-only for query execution (H-001 hardening).
        self._db_uri = "file:catalogue_{:x}?mode=memory&cache=shared".format(
            id(self),
        )
        self._conn_rw = sqlite3.connect(
            self._db_uri, uri=True, check_same_thread=False,
        )
        self._conn_rw.execute("PRAGMA journal_mode = OFF")
        self._conn_rw.execute("PRAGMA synchronous = OFF")
        self._register_functions(self._conn_rw)
        self._create_tables(self._conn_rw)
        self._populate(self._conn_rw)

        # Open a second handle for query execution, set to query_only
        # plus a strict authorizer that rejects anything beyond SELECT.
        self._conn = sqlite3.connect(
            self._db_uri,
            uri=True,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA query_only = ON")
        self._conn.set_authorizer(_catalogue_authorizer)
        self._register_functions(self._conn)

    # -------------------------------------------------------------------
    # Custom SQLite functions mimicking PG builtins
    # -------------------------------------------------------------------
    def _register_functions(self, conn: sqlite3.Connection) -> None:
        c = conn

        c.create_function("pg_get_expr", 2, lambda _expr, _relid: None)
        c.create_function("pg_get_expr", 3, lambda _expr, _relid, _pretty: None)
        c.create_function(
            "version", 0, lambda: _VERSION_STRING,
        )
        # F-001-05: PG scalar time functions used by driver / user probes.
        # SQLite has no native now()/current_timestamp() callables, so we
        # register them returning an ISO-8601 UTC timestamp.
        c.create_function("now", 0, lambda: _utc_now_iso())
        c.create_function("current_timestamp", 0, lambda: _utc_now_iso())
        c.create_function("transaction_timestamp", 0, lambda: _utc_now_iso())
        c.create_function("statement_timestamp", 0, lambda: _utc_now_iso())
        c.create_function("clock_timestamp", 0, lambda: _utc_now_iso())
        c.create_function("current_schema", 0, lambda: "public")
        c.create_function("current_database", 0, lambda: self._tenant_slug)
        c.create_function(
            "current_setting", 1,
            lambda name: "15.0" if name == "server_version" else "",
        )
        c.create_function("pg_get_userbyid", 1, lambda _oid: "tessallite")
        c.create_function(
            "has_schema_privilege", -1, lambda *_args: 1,
        )
        c.create_function(
            "has_table_privilege", -1, lambda *_args: 1,
        )
        c.create_function(
            "has_database_privilege", -1, lambda *_args: 1,
        )
        c.create_function(
            "has_any_column_privilege", -1, lambda *_args: 1,
        )

        # Build a simple OID → type name map for format_type
        type_name_map = {str(oid): name for oid, name, _, _ in _PG_TYPES}

        def _format_type(oid, _mod):
            if oid is None:
                return "unknown"
            return type_name_map.get(str(oid), "unknown")

        c.create_function("format_type", 2, _format_type)

        def _obj_description(oid, _catalog_name=None):
            if oid is None:
                return None
            oid_val = int(oid)
            idx = oid_val - _TABLE_OID_BASE
            if 0 <= idx < len(self._model_names):
                name = self._model_names[idx]
                return self._table_descriptions.get(name)
            return None

        c.create_function("obj_description", -1, _obj_description)

        def _col_description(relid, attnum):
            if relid is None or attnum is None:
                return None
            idx = int(relid) - _TABLE_OID_BASE
            if not (0 <= idx < len(self._model_names)):
                return None
            table_name = self._model_names[idx]
            cols = self._table_columns.get(table_name, [])
            col_idx = int(attnum) - 1
            if not (0 <= col_idx < len(cols)):
                return None
            col = cols[col_idx]
            display_name = col.get("display_name") or col.get("name", "")
            description_text = col.get("description") or ""
            if display_name and display_name != col.get("name") and description_text:
                comment = f"{display_name} — {description_text}"
            elif display_name and display_name != col.get("name"):
                comment = display_name
            else:
                comment = description_text or None
            trust_footer = _build_trust_footer(
                self._table_trust_meta.get(table_name),
            )
            if trust_footer:
                comment = f"{comment}\n{trust_footer}" if comment else trust_footer
            return comment

        c.create_function("col_description", 2, _col_description)

        c.create_function(
            "array_to_string", -1, lambda arr, _sep=",": str(arr) if arr else "",
        )
        c.create_function("pg_encoding_to_char", 1, lambda _enc: "UTF8")
        c.create_function("pg_get_constraintdef", -1, lambda *_args: "")
        c.create_function("pg_get_indexdef", -1, lambda *_args: "")
        c.create_function("pg_total_relation_size", 1, lambda _oid: 0)
        c.create_function("pg_relation_size", -1, lambda *_args: 0)
        c.create_function("pg_size_pretty", 1, lambda _size: "0 bytes")
        c.create_function("shobj_description", 2, lambda _oid, _cat: None)
        c.create_function("pg_get_viewdef", -1, lambda *_args: "")
        c.create_function("pg_get_partkeydef", 1, lambda _oid: None)
        c.create_function("pg_tablespace_location", 1, lambda _oid: "")
        c.create_function("pg_get_serial_sequence", 2, lambda _t, _c: None)
        c.create_function("pg_get_function_result", 1, lambda _oid: "")
        c.create_function("pg_get_function_arguments", 1, lambda _oid: "")
        c.create_function("pg_get_function_identity_arguments", 1, lambda _oid: "")
        c.create_function("session_user_fn", 0, lambda: "tessallite")

        # Aggregate functions
        c.create_aggregate("string_agg", 2, _StringAgg)

    # -------------------------------------------------------------------
    # DDL: create all catalogue tables
    # -------------------------------------------------------------------
    def _create_tables(self, conn: sqlite3.Connection) -> None:
        for ddl in _CATALOGUE_DDL:
            conn.execute(ddl)
        conn.commit()

    # -------------------------------------------------------------------
    # Populate tables from model metadata
    # -------------------------------------------------------------------
    def _populate(self, conn: sqlite3.Connection) -> None:
        self._populate_pg_namespace(conn)
        self._populate_pg_type(conn)
        self._populate_pg_database(conn)
        self._populate_pg_settings(conn)
        self._populate_pg_roles(conn)
        self._populate_pg_tablespace(conn)
        self._populate_pg_proc(conn)
        self._populate_pg_class(conn)
        self._populate_pg_attribute(conn)
        self._populate_pg_attrdef(conn)
        self._populate_pg_description(conn)
        self._populate_pg_index(conn)
        self._populate_pg_constraint(conn)
        self._populate_information_schema(conn)
        self._populate_info_views(conn)
        self._populate_pg_tables(conn)
        self._populate_pg_get_keywords(conn)
        conn.commit()

    def _populate_pg_namespace(self, c: sqlite3.Connection) -> None:
        c.execute(
            "INSERT INTO pg_namespace VALUES (?, ?, ?, ?, ?)",
            (11, "pg_catalog", 10, None, "system catalog schema"),
        )
        c.execute(
            "INSERT INTO pg_namespace VALUES (?, ?, ?, ?, ?)",
            (2200, "public", 10, None,
             "standard public schema (empty)"),
        )
        c.execute(
            "INSERT INTO pg_namespace VALUES (?, ?, ?, ?, ?)",
            (2201, "info", 10, None,
             "Tessallite trust signals (freshness, lineage, owners)"),
        )
        c.execute(
            "INSERT INTO pg_namespace VALUES (?, ?, ?, ?, ?)",
            (11711, "information_schema", 10, None,
             "information schema"),
        )
        # One namespace per project slug
        for slug, oid in self._project_schema_oid.items():
            c.execute(
                "INSERT INTO pg_namespace VALUES (?, ?, ?, ?, ?)",
                (oid, slug, 10, None,
                 f"Tessallite project: {slug}"),
            )

    def _populate_pg_type(self, c: sqlite3.Connection) -> None:
        for oid, typname, typtype, typlen in _PG_TYPES:
            cat = _TYPE_CATEGORIES.get(typname, "U")
            c.execute(
                "INSERT INTO pg_type VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (oid, typname, 11, 10, typtype, cat,
                 0, 0, typlen, "f", "t", 0, -1, "f",
                 f"{typname}in", f"{typname}out", None),
            )

    def _populate_pg_database(self, c: sqlite3.Connection) -> None:
        c.execute(
            "INSERT INTO pg_database VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, self._tenant_slug, 10, 6, "en_US.UTF-8", "en_US.UTF-8",
             "f", "t", -1, 0, 0, 1, 1663, None, None),
        )

    def _populate_pg_settings(self, c: sqlite3.Connection) -> None:
        rows = [
            ("max_identifier_length", "63", None, "Preset Options",
             "Shows the maximum identifier length.", None, "internal",
             "integer", "default", "63", "63", None, "63", "63",
             None, None, "f"),
            ("standard_conforming_strings", "on", None, "Compatibility",
             "Causes strings to treat backslashes literally.", None, "user",
             "bool", "default", None, None, "{on,off}", "on", "on",
             None, None, "f"),
        ]
        c.executemany(
            "INSERT INTO pg_settings VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    def _populate_pg_roles(self, c: sqlite3.Connection) -> None:
        c.execute(
            "INSERT INTO pg_roles VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (10, "tessallite", "t", "t", "t", "t", "t", "f", -1,
             "f", None, None, None),
        )

    def _populate_pg_tablespace(self, c: sqlite3.Connection) -> None:
        c.execute(
            "INSERT INTO pg_tablespace VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1663, "pg_default", 10, None, None, 0, "", None),
        )

    def _populate_pg_proc(self, c: sqlite3.Connection) -> None:
        aggs = [
            ("sum", _OID_NUMERIC, _OID_NUMERIC),
            ("avg", _OID_NUMERIC, _OID_NUMERIC),
            ("min", _OID_NUMERIC, _OID_NUMERIC),
            ("max", _OID_NUMERIC, _OID_NUMERIC),
            ("count", _OID_INT8, _OID_TEXT),
            ("count_distinct", _OID_INT8, _OID_TEXT),
        ]
        for ordinal, (name, ret, arg) in enumerate(aggs, start=1):
            c.execute(
                "INSERT INTO pg_proc VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (210000 + ordinal, name, PUBLIC_SCHEMA_OID, 10,
                 1, ret, "t", "a", str(arg)),
            )

    def _schema_for(self, table_name: str) -> tuple[int, str]:
        """Return (namespace_oid, schema_name) for a table."""
        slug = self._table_project_slug.get(table_name)
        if slug and slug in self._project_schema_oid:
            return self._project_schema_oid[slug], slug
        return PUBLIC_SCHEMA_OID, "public"

    def _populate_pg_class(self, c: sqlite3.Connection) -> None:
        if len(self._model_names) > 80000:
            logger.error(
                "Catalogue OID space at risk: %d tables exceed the safe "
                "range (80000). Index/constraint OIDs may collide.",
                len(self._model_names),
            )
        for i, name in enumerate(self._model_names):
            oid = _TABLE_OID_BASE + i
            ns_oid, _ns_name = self._schema_for(name)
            cols = self._table_columns.get(name, [])
            natts = len(cols)
            has_index = any(col.get("is_primary_key") for col in cols)
            row_est = self._table_row_estimates.get(name) or 0
            desc = self._table_descriptions.get(name) or None
            c.execute(
                "INSERT INTO pg_class VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (oid, name, ns_oid, "r",
                 oid, 0, 10, 0, 0, row_est,
                 "t" if has_index else "f", "f", "p",
                 natts, 0, "f", "f", "f", "f", "f", "t", "d", "f",
                 0, 1, None, None, None, desc),
            )

    def _populate_pg_attribute(self, c: sqlite3.Connection) -> None:
        for i, tname in enumerate(self._model_names):
            toid = _TABLE_OID_BASE + i
            trust_footer = _build_trust_footer(
                self._table_trust_meta.get(tname),
            )
            for attnum, col in enumerate(
                self._table_columns.get(tname, []), start=1,
            ):
                type_oid = _type_oid(col.get("data_type", "text"))
                display_name = col.get("display_name") or col.get("name", "")
                description_text = col.get("description") or ""
                if (display_name and display_name != col.get("name")
                        and description_text):
                    comment = f"{display_name} — {description_text}"
                elif display_name and display_name != col.get("name"):
                    comment = display_name
                else:
                    comment = description_text or None
                if trust_footer:
                    comment = (
                        f"{comment}\n{trust_footer}" if comment
                        else trust_footer
                    )
                c.execute(
                    "INSERT INTO pg_attribute VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (tname, toid, col.get("name", f"col{attnum}"),
                     type_oid, -1, -1, attnum, 0, -1, -1,
                     "f", "x", "i", "f", "f", "f", "", "", "f",
                     "t", 0, 0, None, None, None, None, comment),
                )

    def _populate_pg_attrdef(self, c: sqlite3.Connection) -> None:
        pass  # No column defaults in semantic models

    def _populate_pg_description(self, c: sqlite3.Connection) -> None:
        for i, name in enumerate(self._model_names):
            desc = self._table_descriptions.get(name)
            if desc:
                c.execute(
                    "INSERT INTO pg_description VALUES (?, ?, ?, ?)",
                    (_TABLE_OID_BASE + i, 0, 0, desc),
                )

    def _populate_pg_index(self, c: sqlite3.Connection) -> None:
        for i, tname in enumerate(self._model_names):
            table_oid = _TABLE_OID_BASE + i
            positions = [
                str(pos)
                for pos, col in enumerate(
                    self._table_columns.get(tname, []), start=1,
                )
                if col.get("is_primary_key")
            ]
            if positions:
                c.execute(
                    "INSERT INTO pg_index VALUES (?, ?, ?, ?, ?, ?)",
                    (table_oid + 100000, table_oid, len(positions),
                     "t", "t", " ".join(positions)),
                )

    def _populate_pg_constraint(self, c: sqlite3.Connection) -> None:
        oid_map = {
            name: _TABLE_OID_BASE + i
            for i, name in enumerate(self._model_names)
        }
        # Primary key constraints
        for i, tname in enumerate(self._model_names):
            table_oid = _TABLE_OID_BASE + i
            positions = [
                str(pos)
                for pos, col in enumerate(
                    self._table_columns.get(tname, []), start=1,
                )
                if col.get("is_primary_key")
            ]
            if positions:
                c.execute(
                    "INSERT INTO pg_constraint VALUES "
                    "(?, ?, ?, ?, ?, ?, ?)",
                    (table_oid + 200000, f"{tname}_pkey", "p",
                     table_oid, " ".join(positions), None, None),
                )
        # Foreign key constraints
        for tname, fks in self._table_foreign_keys.items():
            source_oid = oid_map.get(tname)
            if source_oid is None:
                continue
            source_cols = self._table_columns.get(tname, [])
            for ordinal, fk in enumerate(fks, start=1):
                target_name = fk["foreign_table_name"]
                target_oid = oid_map.get(target_name)
                source_pos = next((
                    idx for idx, col in enumerate(source_cols, start=1)
                    if col.get("name") == fk["column_name"]
                ), None)
                target_pos = next((
                    idx for idx, col in enumerate(
                        self._table_columns.get(target_name, []), start=1,
                    )
                    if col.get("name") == fk["foreign_column_name"]
                ), None)
                if target_oid is None or source_pos is None or target_pos is None:
                    continue
                c.execute(
                    "INSERT INTO pg_constraint VALUES "
                    "(?, ?, ?, ?, ?, ?, ?)",
                    (source_oid + 300000 + ordinal,
                     f"{tname}_fkey_{ordinal}", "f",
                     source_oid, str(source_pos),
                     target_oid, str(target_pos)),
                )

    def _populate_information_schema(self, c: sqlite3.Connection) -> None:
        # schemata
        db = self._tenant_slug
        c.execute(
            "INSERT INTO information_schema_schemata VALUES (?, ?, ?)",
            (db, "public", db),
        )
        c.execute(
            "INSERT INTO information_schema_schemata VALUES (?, ?, ?)",
            (db, "info", db),
        )
        for slug in self._project_schema_oid:
            c.execute(
                "INSERT INTO information_schema_schemata VALUES (?, ?, ?)",
                (db, slug, db),
            )

        # tables
        # Register under both project-specific schema AND public schema
        # for compatibility with tools like Looker Studio that only query
        # WHERE TABLE_SCHEMA = 'public'
        for name in self._model_names:
            _ns_oid, schema = self._schema_for(name)
            c.execute(
                "INSERT INTO information_schema_tables VALUES "
                "(?, ?, ?, ?)",
                (db, schema, name, "BASE TABLE"),
            )
            # Also register under 'public' for tools that don't support schema selection
            if schema != "public":
                c.execute(
                    "INSERT INTO information_schema_tables VALUES "
                    "(?, ?, ?, ?)",
                    (db, "public", name, "BASE TABLE"),
                )

        # columns
        # Register under both project-specific schema AND public schema
        for tname in self._model_names:
            _ns_oid, schema = self._schema_for(tname)
            for ordinal, col in enumerate(
                self._table_columns.get(tname, []), start=1,
            ):
                dt = col.get("data_type") or "text"
                pg_type = _TYPE_OID_TO_NAME.get(_type_oid(dt), dt)
                nprecision, nscale = _numeric_metadata(dt)
                c.execute(
                    "INSERT INTO information_schema_columns VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (db, schema, tname,
                     col.get("name", f"col{ordinal}"),
                     ordinal, None,
                     "YES" if col.get("is_nullable", True) else "NO",
                     pg_type, nprecision, nscale),
                )
                # Also register under 'public' for tools that don't support schema selection
                if schema != "public":
                    c.execute(
                        "INSERT INTO information_schema_columns VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (db, "public", tname,
                         col.get("name", f"col{ordinal}"),
                         ordinal, None,
                         "YES" if col.get("is_nullable", True) else "NO",
                         pg_type, nprecision, nscale),
                    )

        # table_constraints + key_column_usage (PKs)
        for tname in self._model_names:
            _ns_oid, schema = self._schema_for(tname)
            key_cols = [
                col for col in self._table_columns.get(tname, [])
                if col.get("is_primary_key")
            ]
            if key_cols:
                c.execute(
                    "INSERT INTO information_schema_table_constraints VALUES "
                    "(?, ?, ?, ?, ?)",
                    (db, schema, tname,
                     f"{tname}_pkey", "PRIMARY KEY"),
                )
                for ordinal, col in enumerate(key_cols, start=1):
                    c.execute(
                        "INSERT INTO information_schema_key_column_usage "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (db, schema, tname,
                         f"{tname}_pkey",
                         col.get("name", ""), ordinal),
                    )

        # referential_constraints
        for tname, fks in self._table_foreign_keys.items():
            _ns_oid, schema = self._schema_for(tname)
            for ordinal, fk in enumerate(fks, start=1):
                c.execute(
                    "INSERT INTO information_schema_referential_constraints "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (db, schema, tname,
                     f"{tname}_fkey_{ordinal}",
                     fk["column_name"],
                     fk["foreign_table_name"],
                     fk["foreign_column_name"],
                     "NO ACTION", "NO ACTION"),
                )

    # Info view definitions for catalogue discovery registration.
    _INFO_VIEWS: list[tuple[str, list[tuple[str, str]]]] = [
        ("model_freshness", [
            ("model_name", "text"), ("measure_name", "text"),
            ("last_refreshed_at", "text"), ("source_system", "text"),
        ]),
        ("model_lineage", [
            ("model_name", "text"), ("object_type", "text"),
            ("object_name", "text"), ("source_table", "text"),
            ("source_column", "text"),
        ]),
        ("model_owners", [
            ("model_name", "text"), ("owner_user_email", "text"),
            ("owner_team", "text"),
        ]),
    ]

    def _populate_info_views(self, c: sqlite3.Connection) -> None:
        """Populate info_model_freshness, info_model_lineage, info_model_owners
        and register them in information_schema_tables, pg_class, and pg_tables
        so they are discoverable by DBeaver and other JDBC clients.
        """
        db = self._tenant_slug

        # Register each info view in discovery catalogues.
        info_view_oid_base = _TABLE_OID_BASE + 90000
        for vi, (view_name, view_cols) in enumerate(self._INFO_VIEWS):
            view_oid = info_view_oid_base + vi
            # information_schema_tables
            c.execute(
                "INSERT INTO information_schema_tables VALUES (?, ?, ?, ?)",
                (db, "info", view_name, "VIEW"),
            )
            # pg_class (relkind='v' for view)
            c.execute(
                "INSERT INTO pg_class VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (view_oid, view_name, _INFO_SCHEMA_OID, "v",
                 view_oid, 0, 10, 0, 0, 0,
                 "f", "f", "p",
                 len(view_cols), 0, "f", "f", "f", "f", "f", "t", "d", "f",
                 0, 1, None, None, None,
                 f"Tessallite trust signal: {view_name}"),
            )
            # pg_attribute for each column of the info view
            for attnum, (col_name, col_type) in enumerate(view_cols, start=1):
                c.execute(
                    "INSERT INTO pg_attribute VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (view_name, view_oid, col_name,
                     _type_oid(col_type), -1, -1, attnum, 0, -1, -1,
                     "f", "x", "i", "f", "f", "f", "", "", "f",
                     "t", 0, 0, None, None, None, None, None),
                )
            # pg_tables
            c.execute(
                "INSERT INTO pg_tables VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("info", view_name, "tessallite", None, "f", "f", "f", "f"),
            )
            # information_schema_columns
            for ordinal, (col_name, col_type) in enumerate(view_cols, start=1):
                pg_type = _TYPE_OID_TO_NAME.get(_type_oid(col_type), col_type)
                c.execute(
                    "INSERT INTO information_schema_columns VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (db, "info", view_name, col_name,
                     ordinal, None, "YES", pg_type, None, None),
                )

        emitted: set[str] = set()
        for name in self._model_names:
            canonical = self._canonical_for(name)
            if canonical in emitted:
                continue
            emitted.add(canonical)
            trust = (
                self._table_trust_meta.get(name)
                or self._table_trust_meta.get(canonical)
                or {}
            )
            last = trust.get("last_refreshed_at") or ""
            source = trust.get("source_system") or ""
            owner = trust.get("owner") or ""

            # freshness: one row per measure
            for col in self._table_columns.get(name, []):
                if col.get("kind") != "measure":
                    continue
                c.execute(
                    "INSERT INTO info_model_freshness VALUES (?, ?, ?, ?)",
                    (canonical, col.get("name", ""),
                     str(last) if last else None,
                     str(source) if source else None),
                )

            # lineage: one row per column/measure
            for col in self._table_columns.get(name, []):
                kind = col.get("kind") or "column"
                c.execute(
                    "INSERT INTO info_model_lineage VALUES (?, ?, ?, ?, ?)",
                    (canonical, kind, col.get("name", ""),
                     col.get("source_table") or canonical,
                     col.get("source_column") or col.get("name", "")),
                )

            # owners
            c.execute(
                "INSERT INTO info_model_owners VALUES (?, ?, ?)",
                (canonical, str(owner) if owner else None, None),
            )

    def _populate_pg_tables(self, c: sqlite3.Connection) -> None:
        """pg_tables view — used by some drivers instead of pg_class."""
        for name in self._model_names:
            _ns_oid, schema = self._schema_for(name)
            cols = self._table_columns.get(name, [])
            has_index = any(col.get("is_primary_key") for col in cols)
            c.execute(
                "INSERT INTO pg_tables VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (schema, name, "tessallite", None,
                 "t" if has_index else "f", "f", "f", "f"),
            )
            # Also register under 'public' for tools that don't support schema selection
            if schema != "public":
                c.execute(
                    "INSERT INTO pg_tables VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("public", name, "tessallite", None,
                     "t" if has_index else "f", "f", "f", "f"),
                )

    def _populate_pg_get_keywords(self, c: sqlite3.Connection) -> None:
        """pg_get_keywords — pgJDBC calls this during connection setup."""
        c.executemany(
            "INSERT INTO pg_get_keywords VALUES (?, ?, ?)",
            _PG_KEYWORDS,
        )

    def _canonical_for(self, name: str) -> str:
        if self._table_model_id:
            target_mid = self._table_model_id.get(name)
            if target_mid:
                siblings = [
                    cand for cand, mid in self._table_model_id.items()
                    if mid == target_mid
                ]
                if siblings:
                    return min(siblings, key=len)
        best = name
        for base in self._model_names:
            if base == name or name.startswith(base + "_"):
                if len(base) < len(best):
                    best = base
        return best

    # -------------------------------------------------------------------
    # Public API: execute catalogue SQL
    # -------------------------------------------------------------------
    def _references_catalogue(self, sql: str) -> bool:
        """AST check: does *sql* genuinely reference catalogue objects?

        F-001-03: the routing decision is made from the parsed *table*
        references, not a substring of the raw text:

          * any referenced table/schema is a catalogue object  → catalogue;
          * otherwise any referenced table is a model relation → forward
            (a token like ``pg_class`` inside a string literal lives in a
            Literal node, not a Table, so it cannot hijack the query);
          * no table references at all (a FROM-less metadata probe such as
            ``SELECT version()``) while the regex matched → catalogue.

        When sqlglot cannot parse the SQL we fall back to the regex result
        (True) — the conservative, pre-existing path.
        """
        try:
            statements = sqlglot.parse(sql, read="postgres")
        except Exception:
            return True  # cannot parse → keep legacy regex routing
        if not statements or statements[0] is None:
            return True

        saw_table = False
        for statement in statements:
            for table in statement.find_all(exp.Table):
                saw_table = True
                schema = (table.db or "").lower()
                if schema in _CATALOGUE_SCHEMAS:
                    return True
                name = (table.name or "").lower()
                if name in _CATALOGUE_TABLE_NAMES:
                    return True
                if any(name.startswith(p) for p in _CATALOGUE_TABLE_PREFIXES):
                    return True
        # A query that references model tables (and no catalogue object) is a
        # user data query — forward it even if a token appears in a literal.
        if saw_table:
            return False
        # No table references: a FROM-less metadata probe. The regex already
        # confirmed a catalogue token, so route it to the catalogue engine.
        return True

    def execute(
        self, sql: str,
    ) -> tuple[list[tuple[str, int]], list[list[str | None]]] | None:
        """Execute SQL against the catalogue if it references system tables.

        Returns ``(col_desc_with_oids, rows)`` on match, or ``None`` if the
        SQL should be forwarded to the query-router.

        F-001-03: routing is decided by AST inspection of referenced relations
        and functions, not by a substring scan of raw text — so a model query
        whose literal merely contains ``pg_class`` or ``current_setting(`` is
        no longer hijacked to the catalogue engine. The cheap regex remains a
        fast pre-filter (no catalogue tokens at all → forward immediately).
        """
        if not _CATALOGUE_RE.search(sql):
            return None
        if not self._references_catalogue(sql):
            # Regex matched only inside a string literal / model relation;
            # this is a user data query — forward to the router.
            return None

        transformed = _transform_sql(sql)

        try:
            cursor = self._conn.execute(transformed)
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            # F-001-03: the query was genuinely catalogue-shaped but failed in
            # SQLite. Surface the failure as an error instead of fabricating a
            # successful empty result that silently hides the problem.
            logger.warning(
                "Catalogue SQL failed (%s): %s", exc, transformed[:300],
            )
            raise CatalogueQueryError(str(exc)) from exc

        if cursor.description is None:
            return [("result", _OID_TEXT)], []

        col_desc = [
            (
                desc[0],
                _COLUMN_OID_HINTS.get(desc[0], _OID_TEXT),
            )
            for desc in cursor.description
        ]

        rows = [
            [str(v) if v is not None else None for v in row]
            for row in cursor.fetchall()
        ]

        return col_desc, rows

    def evaluate_constant_select(
        self, sql: str,
    ) -> tuple[list[tuple[str, int]], list[list[str | None]]] | None:
        """Evaluate a FROM-less scalar SELECT through the read-only engine.

        F-001-05: used for expressions the gateway will not echo as text
        (``SELECT 1+1``, ``SELECT now()``). Runs against the read-only
        connection (authorizer + ``query_only`` still apply). Returns
        ``(col_desc, rows)`` on success, or ``None`` when SQLite cannot
        evaluate it (caller then returns a clear unsupported error). Any SQL
        that touches a table is rejected by the authorizer and yields None.
        """
        try:
            cursor = self._conn.execute(_transform_sql(sql))
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            logger.debug("Constant SELECT not evaluable by catalogue: %s (%s)", sql[:200], exc)
            return None
        if cursor.description is None:
            return None
        col_desc = [
            (desc[0] if desc[0] else "?column?", _OID_TEXT)
            for desc in cursor.description
        ]
        rows = [
            [str(v) if v is not None else None for v in row]
            for row in cursor.fetchall()
        ]
        return col_desc, rows

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
        try:
            self._conn_rw.close()
        except Exception:
            pass


# -----------------------------------------------------------------------
# DDL for all catalogue tables
# -----------------------------------------------------------------------
_CATALOGUE_DDL: list[str] = [
    # -- pg_catalog core tables (populated) --
    """CREATE TABLE pg_namespace (
        oid INTEGER, nspname TEXT, nspowner INTEGER,
        nspacl TEXT, description TEXT
    )""",
    """CREATE TABLE pg_class (
        oid INTEGER, relname TEXT, relnamespace INTEGER, relkind TEXT,
        reltype INTEGER, reloftype INTEGER, relowner INTEGER, relam INTEGER,
        relpages INTEGER, reltuples REAL,
        relhasindex TEXT, relisshared TEXT, relpersistence TEXT,
        relnatts INTEGER, relchecks INTEGER,
        relhasrules TEXT, relhastriggers TEXT, relhassubclass TEXT,
        relrowsecurity TEXT, relforcerowsecurity TEXT,
        relispopulated TEXT, relreplident TEXT, relispartition TEXT,
        relfrozenxid INTEGER, relminmxid INTEGER,
        relacl TEXT, reloptions TEXT, relpartbound TEXT,
        description TEXT
    )""",
    """CREATE TABLE pg_attribute (
        relname TEXT, attrelid INTEGER, attname TEXT,
        atttypid INTEGER, attstattarget INTEGER, attlen INTEGER,
        attnum INTEGER, attndims INTEGER, attcacheoff INTEGER,
        atttypmod INTEGER,
        attbyval TEXT, attstorage TEXT, attalign TEXT,
        attnotnull TEXT, atthasdef TEXT, atthasmissing TEXT,
        attidentity TEXT, attgenerated TEXT, attisdropped TEXT,
        attislocal TEXT, attinhcount INTEGER, attcollation INTEGER,
        attacl TEXT, attoptions TEXT, attfdwoptions TEXT,
        def_value TEXT, description TEXT
    )""",
    """CREATE TABLE pg_type (
        oid INTEGER, typname TEXT, typnamespace INTEGER,
        typowner INTEGER, typtype TEXT, typcategory TEXT,
        typelem INTEGER, typrelid INTEGER, typlen INTEGER,
        typbyval TEXT, typisdefined TEXT, typbasetype INTEGER,
        typtypmod INTEGER, typnotnull TEXT,
        typinput TEXT, typoutput TEXT, description TEXT
    )""",
    """CREATE TABLE pg_attrdef (
        oid INTEGER, adrelid INTEGER, adnum INTEGER, adbin TEXT
    )""",
    """CREATE TABLE pg_description (
        objoid INTEGER, classoid INTEGER, objsubid INTEGER,
        description TEXT
    )""",
    """CREATE TABLE pg_database (
        oid INTEGER, datname TEXT, datdba INTEGER, encoding INTEGER,
        datcollate TEXT, datctype TEXT,
        datistemplate TEXT, datallowconn TEXT, datconnlimit INTEGER,
        datlastsysoid INTEGER, datfrozenxid INTEGER, datminmxid INTEGER,
        dattablespace INTEGER, datacl TEXT, description TEXT
    )""",
    """CREATE TABLE pg_settings (
        name TEXT, setting TEXT, unit TEXT, category TEXT,
        short_desc TEXT, extra_desc TEXT, context TEXT,
        vartype TEXT, source TEXT, min_val TEXT, max_val TEXT,
        enumvals TEXT, boot_val TEXT, reset_val TEXT,
        sourcefile TEXT, sourceline INTEGER, pending_restart TEXT
    )""",
    """CREATE TABLE pg_roles (
        oid INTEGER, rolname TEXT, rolsuper TEXT, rolinherit TEXT,
        rolcreaterole TEXT, rolcreatedb TEXT, rolcanlogin TEXT,
        rolreplication TEXT, rolconnlimit INTEGER, rolbypassrls TEXT,
        rolvaliduntil TEXT, memberof TEXT, description TEXT
    )""",
    """CREATE TABLE pg_tablespace (
        oid INTEGER, spcname TEXT, spcowner INTEGER,
        spcacl TEXT, spcoptions TEXT, spcmaxbytes INTEGER,
        location TEXT, description TEXT
    )""",
    """CREATE TABLE pg_index (
        indexrelid INTEGER, indrelid INTEGER, indnatts INTEGER,
        indisunique TEXT, indisprimary TEXT, indkey TEXT
    )""",
    """CREATE TABLE pg_constraint (
        oid INTEGER, conname TEXT, contype TEXT,
        conrelid INTEGER, conkey TEXT,
        confrelid INTEGER, confkey TEXT
    )""",
    """CREATE TABLE pg_proc (
        oid INTEGER, proname TEXT, pronamespace INTEGER,
        proowner INTEGER, pronargs INTEGER, prorettype INTEGER,
        proisagg TEXT, prokind TEXT, proargtypes TEXT
    )""",
    # -- pg_catalog empty tables (exist for JOIN compatibility) --
    """CREATE TABLE pg_am (oid INTEGER, amname TEXT, amhandler TEXT, amtype TEXT)""",
    """CREATE TABLE pg_amop (oid INTEGER, amopfamily INTEGER, amoplefttype INTEGER, amoprighttype INTEGER, amopstrategy INTEGER, amopopr INTEGER, amopmethod INTEGER)""",
    """CREATE TABLE pg_amproc (oid INTEGER, amprocfamily INTEGER, amproclefttype INTEGER, amprocrighttype INTEGER, amprocnum INTEGER, amproc TEXT)""",
    """CREATE TABLE pg_auth_members (roleid INTEGER, member INTEGER, grantor INTEGER, admin_option TEXT)""",
    """CREATE TABLE pg_cast (oid INTEGER, castsource INTEGER, casttarget INTEGER, castfunc INTEGER, castcontext TEXT, castmethod TEXT)""",
    """CREATE TABLE pg_collation (oid INTEGER, collname TEXT, collnamespace INTEGER, collowner INTEGER, collprovider TEXT, collencoding INTEGER, collcollate TEXT, collctype TEXT)""",
    """CREATE TABLE pg_conversion (oid INTEGER, conname TEXT, connamespace INTEGER, conowner INTEGER, conforencoding INTEGER, contoencoding INTEGER, conproc TEXT, condefault TEXT)""",
    """CREATE TABLE pg_default_acl (oid INTEGER, defaclrole INTEGER, defaclnamespace INTEGER, defaclobjtype TEXT, defaclacl TEXT)""",
    """CREATE TABLE pg_depend (classid INTEGER, objid INTEGER, objsubid INTEGER, refclassid INTEGER, refobjid INTEGER, refobjsubid INTEGER, deptype TEXT)""",
    """CREATE TABLE pg_enum (oid INTEGER, enumtypid INTEGER, enumsortorder REAL, enumlabel TEXT)""",
    """CREATE TABLE pg_event_trigger (oid INTEGER, evtname TEXT, evtevent TEXT, evtowner INTEGER, evtfoid INTEGER, evtenabled TEXT, evttags TEXT)""",
    """CREATE TABLE pg_extension (oid INTEGER, extname TEXT, extowner INTEGER, extnamespace INTEGER, extrelocatable TEXT, extversion TEXT, extconfig TEXT, extcondition TEXT)""",
    """CREATE TABLE pg_foreign_data_wrapper (oid INTEGER, fdwname TEXT, fdwowner INTEGER, fdwhandler INTEGER, fdwvalidator INTEGER, fdwacl TEXT, fdwoptions TEXT)""",
    """CREATE TABLE pg_foreign_server (oid INTEGER, srvname TEXT, srvowner INTEGER, srvfdw INTEGER, srvtype TEXT, srvversion TEXT, srvacl TEXT, srvoptions TEXT)""",
    """CREATE TABLE pg_foreign_table (ftrelid INTEGER, ftserver INTEGER, ftoptions TEXT)""",
    """CREATE TABLE pg_inherits (inhrelid INTEGER, inhparent INTEGER, inhseqno INTEGER)""",
    """CREATE TABLE pg_language (oid INTEGER, lanname TEXT, lanowner INTEGER, lanispl TEXT, lanpltrusted TEXT, lanplcallfoid INTEGER, laninline INTEGER, lanvalidator INTEGER, lanacl TEXT)""",
    """CREATE TABLE pg_largeobject (loid INTEGER, pageno INTEGER, data BLOB)""",
    """CREATE TABLE pg_matviews (schemaname TEXT, matviewname TEXT, matviewowner TEXT, tablespace TEXT, hasindexes TEXT, ispopulated TEXT, definition TEXT)""",
    """CREATE TABLE pg_opclass (oid INTEGER, opcmethod INTEGER, opcname TEXT, opcnamespace INTEGER, opcowner INTEGER, opcfamily INTEGER, opcintype INTEGER, opcdefault TEXT, opckeytype INTEGER)""",
    """CREATE TABLE pg_operator (oid INTEGER, oprname TEXT, oprnamespace INTEGER, oprowner INTEGER, oprkind TEXT, oprcanmerge TEXT, oprcanhash TEXT, oprleft INTEGER, oprright INTEGER, oprresult INTEGER, oprcom INTEGER, oprnegate INTEGER, oprcode TEXT, oprrest TEXT, oprjoin TEXT)""",
    """CREATE TABLE pg_opfamily (oid INTEGER, opfmethod INTEGER, opfname TEXT, opfnamespace INTEGER, opfowner INTEGER)""",
    """CREATE TABLE pg_policy (oid INTEGER, polname TEXT, polrelid INTEGER, polcmd TEXT, polpermissive TEXT, polroles TEXT, polqual TEXT, polwithcheck TEXT)""",
    """CREATE TABLE pg_publication (oid INTEGER, pubname TEXT, pubowner INTEGER, puballtables TEXT, pubinsert TEXT, pubupdate TEXT, pubdelete TEXT, pubtruncate TEXT)""",
    """CREATE TABLE pg_range (rngtypid INTEGER, rngsubtype INTEGER, rngmultitypid INTEGER, rngcollation INTEGER, rngsubopc INTEGER, rngcanonical TEXT, rngsubdiff TEXT)""",
    """CREATE TABLE pg_rewrite (oid INTEGER, rulename TEXT, ev_class INTEGER, ev_type TEXT, ev_enabled TEXT, is_instead TEXT, ev_qual TEXT, ev_action TEXT)""",
    """CREATE TABLE pg_seclabel (objoid INTEGER, classoid INTEGER, objsubid INTEGER, provider TEXT, label TEXT)""",
    """CREATE TABLE pg_sequence (seqrelid INTEGER, seqtypid INTEGER, seqstart INTEGER, seqincrement INTEGER, seqmax INTEGER, seqmin INTEGER, seqcache INTEGER, seqcycle TEXT)""",
    """CREATE TABLE pg_shdescription (objoid INTEGER, classoid INTEGER, description TEXT)""",
    """CREATE TABLE pg_shseclabel (objoid INTEGER, classoid INTEGER, provider TEXT, label TEXT)""",
    """CREATE TABLE pg_statistic (starelid INTEGER, staattnum INTEGER, stainherit TEXT, stanullfrac REAL, stawidth INTEGER, stadistinct REAL)""",
    """CREATE TABLE pg_statistic_ext (oid INTEGER, stxrelid INTEGER, stxname TEXT, stxnamespace INTEGER, stxowner INTEGER, stxstattarget INTEGER, stxkeys TEXT, stxkind TEXT)""",
    """CREATE TABLE pg_subscription (oid INTEGER, subdbid INTEGER, subname TEXT, subowner INTEGER, subenabled TEXT, subconninfo TEXT, subslotname TEXT, subsynccommit TEXT, subpublications TEXT)""",
    """CREATE TABLE pg_transform (oid INTEGER, trftype INTEGER, trflang INTEGER, trffromsql TEXT, trftosql TEXT)""",
    """CREATE TABLE pg_trigger (oid INTEGER, tgrelid INTEGER, tgname TEXT, tgfoid INTEGER, tgtype INTEGER, tgenabled TEXT, tgisinternal TEXT, tgconstrrelid INTEGER, tgconstrindid INTEGER, tgconstraint INTEGER, tgdeferrable TEXT, tginitdeferred TEXT, tgnargs INTEGER, tgattr TEXT, tgargs BLOB, tgqual TEXT)""",
    """CREATE TABLE pg_ts_config (oid INTEGER, cfgname TEXT, cfgnamespace INTEGER, cfgowner INTEGER, cfgparser INTEGER)""",
    """CREATE TABLE pg_ts_dict (oid INTEGER, dictname TEXT, dictnamespace INTEGER, dictowner INTEGER, dicttemplate INTEGER, dictinitoption TEXT)""",
    """CREATE TABLE pg_ts_parser (oid INTEGER, prsname TEXT, prsnamespace INTEGER, prsstart TEXT, prstoken TEXT, prsend TEXT, prsheadline TEXT, prslextype TEXT)""",
    """CREATE TABLE pg_ts_template (oid INTEGER, tmplname TEXT, tmplnamespace INTEGER, tmplinit TEXT, tmpllexize TEXT)""",
    """CREATE TABLE pg_user_mapping (oid INTEGER, umuser INTEGER, umserver INTEGER, umoptions TEXT)""",
    """CREATE TABLE pg_views (schemaname TEXT, viewname TEXT, viewowner TEXT, definition TEXT)""",
    # -- information_schema tables (populated) --
    """CREATE TABLE information_schema_schemata (
        catalog_name TEXT, schema_name TEXT, schema_owner TEXT
    )""",
    """CREATE TABLE information_schema_tables (
        table_catalog TEXT, table_schema TEXT,
        table_name TEXT, table_type TEXT
    )""",
    """CREATE TABLE information_schema_columns (
        table_catalog TEXT, table_schema TEXT,
        table_name TEXT, column_name TEXT,
        ordinal_position INTEGER, column_default TEXT,
        is_nullable TEXT, data_type TEXT,
        numeric_precision TEXT, numeric_scale TEXT
    )""",
    """CREATE TABLE information_schema_table_constraints (
        table_catalog TEXT, table_schema TEXT,
        table_name TEXT, constraint_name TEXT, constraint_type TEXT
    )""",
    """CREATE TABLE information_schema_key_column_usage (
        table_catalog TEXT, table_schema TEXT,
        table_name TEXT, constraint_name TEXT,
        column_name TEXT, ordinal_position INTEGER
    )""",
    """CREATE TABLE information_schema_referential_constraints (
        table_catalog TEXT, table_schema TEXT,
        table_name TEXT, constraint_name TEXT,
        column_name TEXT, foreign_table_name TEXT,
        foreign_column_name TEXT, update_rule TEXT, delete_rule TEXT
    )""",
    # -- information_schema empty tables (JOIN compatibility) --
    """CREATE TABLE information_schema_views (
        table_catalog TEXT, table_schema TEXT, table_name TEXT,
        view_definition TEXT, check_option TEXT, is_updatable TEXT
    )""",
    """CREATE TABLE information_schema_triggers (
        trigger_catalog TEXT, trigger_schema TEXT, trigger_name TEXT,
        event_manipulation TEXT, event_object_catalog TEXT,
        event_object_schema TEXT, event_object_table TEXT,
        action_statement TEXT, action_timing TEXT
    )""",
    """CREATE TABLE information_schema_routines (
        routine_catalog TEXT, routine_schema TEXT, routine_name TEXT,
        routine_type TEXT, data_type TEXT, routine_definition TEXT
    )""",
    """CREATE TABLE information_schema_role_table_grants (
        grantor TEXT, grantee TEXT, table_catalog TEXT,
        table_schema TEXT, table_name TEXT, privilege_type TEXT,
        is_grantable TEXT
    )""",
    """CREATE TABLE information_schema_check_constraints (
        constraint_catalog TEXT, constraint_schema TEXT,
        constraint_name TEXT, check_clause TEXT
    )""",
    """CREATE TABLE information_schema_domains (
        domain_catalog TEXT, domain_schema TEXT, domain_name TEXT,
        data_type TEXT, character_maximum_length INTEGER,
        numeric_precision INTEGER, numeric_scale INTEGER
    )""",
    """CREATE TABLE information_schema_sequences (
        sequence_catalog TEXT, sequence_schema TEXT, sequence_name TEXT,
        data_type TEXT, numeric_precision INTEGER,
        start_value TEXT, minimum_value TEXT, maximum_value TEXT,
        increment TEXT, cycle_option TEXT
    )""",
    # -- info.* virtual tables (populated) --
    """CREATE TABLE info_model_freshness (
        model_name TEXT, measure_name TEXT,
        last_refreshed_at TEXT, source_system TEXT
    )""",
    """CREATE TABLE info_model_lineage (
        model_name TEXT, object_type TEXT, object_name TEXT,
        source_table TEXT, source_column TEXT
    )""",
    """CREATE TABLE info_model_owners (
        model_name TEXT, owner_user_email TEXT, owner_team TEXT
    )""",
    # -- pg_tables view as a real table (used by some clients) --
    """CREATE TABLE pg_tables (
        schemaname TEXT, tablename TEXT, tableowner TEXT,
        tablespace TEXT, hasindexes TEXT, hasrules TEXT,
        hastriggers TEXT, rowsecurity TEXT
    )""",
    # -- pg_get_keywords as a table (pgJDBC calls SELECT * FROM pg_get_keywords()) --
    """CREATE TABLE pg_get_keywords (
        word TEXT, catcode TEXT, catdesc TEXT
    )""",
]

# SQL reserved words for pg_get_keywords (subset matching PG 15 output)
_PG_KEYWORDS: list[tuple[str, str, str]] = [
    ("select", "R", "reserved"),
    ("from", "R", "reserved"),
    ("where", "R", "reserved"),
    ("and", "R", "reserved"),
    ("or", "R", "reserved"),
    ("not", "R", "reserved"),
    ("in", "R", "reserved"),
    ("is", "R", "reserved"),
    ("null", "R", "reserved"),
    ("true", "R", "reserved"),
    ("false", "R", "reserved"),
    ("as", "R", "reserved"),
    ("on", "R", "reserved"),
    ("join", "R", "reserved"),
    ("left", "R", "reserved"),
    ("right", "R", "reserved"),
    ("inner", "R", "reserved"),
    ("outer", "R", "reserved"),
    ("cross", "R", "reserved"),
    ("full", "R", "reserved"),
    ("group", "R", "reserved"),
    ("order", "R", "reserved"),
    ("by", "R", "reserved"),
    ("having", "R", "reserved"),
    ("limit", "R", "reserved"),
    ("offset", "R", "reserved"),
    ("union", "R", "reserved"),
    ("all", "R", "reserved"),
    ("distinct", "R", "reserved"),
    ("case", "R", "reserved"),
    ("when", "R", "reserved"),
    ("then", "R", "reserved"),
    ("else", "R", "reserved"),
    ("end", "R", "reserved"),
    ("exists", "R", "reserved"),
    ("between", "R", "reserved"),
    ("like", "R", "reserved"),
    ("insert", "R", "reserved"),
    ("update", "R", "reserved"),
    ("delete", "R", "reserved"),
    ("create", "R", "reserved"),
    ("drop", "R", "reserved"),
    ("alter", "R", "reserved"),
    ("table", "R", "reserved"),
    ("index", "R", "reserved"),
    ("into", "R", "reserved"),
    ("values", "R", "reserved"),
    ("set", "R", "reserved"),
    ("begin", "R", "reserved"),
    ("commit", "R", "reserved"),
    ("rollback", "R", "reserved"),
    ("grant", "R", "reserved"),
    ("revoke", "R", "reserved"),
    ("primary", "R", "reserved"),
    ("foreign", "R", "reserved"),
    ("key", "R", "reserved"),
    ("references", "R", "reserved"),
    ("constraint", "R", "reserved"),
    ("default", "R", "reserved"),
    ("check", "R", "reserved"),
    ("unique", "R", "reserved"),
    ("asc", "R", "reserved"),
    ("desc", "R", "reserved"),
    ("with", "R", "reserved"),
    ("recursive", "R", "reserved"),
    ("cast", "R", "reserved"),
    ("current_date", "R", "reserved"),
    ("current_time", "R", "reserved"),
    ("current_timestamp", "R", "reserved"),
]
