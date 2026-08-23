"""
Expert Directive Compliant MDSCHEMA rowset builder.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from shared.config.bootstrap import system_snapshot_get
from shared.schemas.measure_formats import format_token_to_mdx
# Import branding constants from constants for Expert Directive 2.6
from src.dax.constants import PROVIDER_VERSION, SERVER_NAME
# Bug-6603: single source of the cube SHAPE (dimension -> hierarchy origin) and the
# field-list grouping key (standalone dims -> one [Dimensions] group node).
from src.dax.cube_model import (
    HIERARCHY_GROUP_NAME,
    HIERARCHY_GROUP_UNIQUE_NAME,
    STANDALONE_GROUP_NAME,
    STANDALONE_GROUP_UNIQUE_NAME,
    dimension_unique_name_for,
    hierarchy_origin_for,
    is_grouped_hierarchy,
    is_standalone_attribute,
)
from src.dax.member_uname import (
    ancestor_key_path_from_parent_chain,
    member_filter_matches,
    parse_member_uname,
    qualify_member_uname,
    unescape_member_key,
)
# Bug-9178: Named Query ``@name`` relations are advertised in the XMLA table
# rowsets (DBSCHEMA_TABLES / DBSCHEMA_COLUMNS) with the SAME column builder the
# JDBC catalogue registration uses, so the two channels advertise identical
# column metadata from the deployed snapshot's ``output_columns``.
from src.router_client import build_named_query_relation_columns

logger = logging.getLogger(__name__)


def _escape_mdx_bracket(name: str) -> str:
    """Escape ``]`` inside an MDX bracketed identifier by doubling it.

    Bug-6717: MDX identifier escaping requires ``]`` inside ``[...]`` to be
    doubled (``]]``). A measure name containing ``]`` produces a unique name
    clients cannot round-trip without this escaping. Aligned with the
    excel-plugin's ``escapeMdxBracketContent`` helper.
    """
    return name.replace("]", "]]")


def _meta_created() -> str:
    return str(system_snapshot_get("xmla.metadata_created_at"))


def _meta_modified() -> str:
    return str(system_snapshot_get("xmla.metadata_modified_at"))


def _datasource_url_fallback() -> str:
    return str(system_snapshot_get("xmla.datasource_url_fallback"))

_CFG_PATH = Path(__file__).parent / "mdschema_config.json"
_cfg: dict = json.loads(_CFG_PATH.read_text())


_ROWSETS = _cfg["rowsets"]
_AGG_CODES: dict[str, int] = _cfg["measure_aggregator_codes"]
_TYPE_CODES: dict[str, int] = _cfg["data_type_codes"]
_SCHEMA_GUIDS: dict[str, str] = _cfg.get("schema_guids", {})

_ROWSET_NS = "urn:schemas-microsoft-com:xml-analysis:rowset"
_SQL_NS    = "urn:schemas-microsoft-com:xml-sql"

def build_discover_response(
    request_type: str,
    catalog_name: str,
    model_id: str,
    measures: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
    endpoint_url: str = "",
    properties: dict[str, str] | None = None,
    restrictions: dict[str, list[str]] | None = None,
    tenant_models: list[dict[str, Any]] | None = None,
    member_data: dict[str, dict] | None = None,
    trust_meta: dict[str, Any] | None = None,
    hierarchy_defs: list[dict[str, Any]] | None = None,  # kept for caller compat
    named_sets: list[dict[str, Any]] | None = None,
    kpis: list[dict[str, Any]] | None = None,
    named_queries: list[dict[str, Any]] | None = None,
) -> str:
    rtype = request_type.upper()

    if rtype == "DISCOVER_SCHEMA_ROWSETS":
        return _build_schema_rowsets_xml()

    rows = _get_rows(
        rtype, catalog_name, model_id, measures, dimensions, endpoint_url,
        properties or {}, restrictions or {}, tenant_models or [], member_data or {},
        trust_meta or {}, named_sets or [], kpis or [], named_queries or [],
    )

    col_defs = _ROWSETS[rtype]["columns"] if rtype in _ROWSETS else [{"name": k, "type": "string"} for k in (rows[0].keys() if rows else [])]
    return _build_rowset_xml(col_defs, rows)

# XSD built-in types must be referenced with the ``xs:`` prefix. The inline rowset
# schema is nested under a ``<root xmlns="...rowset">`` element, so an UNPREFIXED
# ``type="string"`` resolves (via the inherited default namespace) to the rowset
# target namespace — where ``string`` is undefined — making the schema invalid.
# Lenient clients (curl, our XMLA probes) ignore it, but Excel's MSOLAP validates
# the schema strictly and rejects the rowset ("cannot retrieve list of databases").
# Custom types declared IN the target namespace (uuid, row, xmlDocument) must stay
# unprefixed so they resolve to the rowset namespace.
_XSD_BUILTIN_TYPES = frozenset({
    "string", "int", "integer", "long", "short", "decimal", "double", "float",
    "boolean", "dateTime", "date", "time", "base64Binary",
    "unsignedInt", "unsignedShort", "unsignedLong", "unsignedByte", "byte",
})
_ROWSET_CUSTOM_TYPES = frozenset({"uuid", "row", "xmlDocument"})


def _qualify_xsd_type(xsd_type: str) -> str:
    """Return the schema type reference, prefixing XSD built-ins with ``xs:``.

    Custom rowset-namespace types (uuid/row/xmlDocument) are returned unchanged.
    An unrecognised type (neither a known built-in nor a custom type) is returned
    as-is but LOGGED — a typo'd or newly-added config type emitted bare would
    silently re-introduce the invalid-schema condition (Bug-5518).
    """
    if xsd_type in _XSD_BUILTIN_TYPES:
        return f"xs:{xsd_type}"
    if xsd_type not in _ROWSET_CUSTOM_TYPES:
        logger.warning(
            "mdschema: unrecognised XSD type %r emitted unprefixed; if it is an "
            "XSD built-in, add it to _XSD_BUILTIN_TYPES (Bug-5518)", xsd_type,
        )
    return xsd_type


def _build_rowset_xml(col_defs: list[dict], rows: list[dict[str, str]]) -> str:
    """
    Render a MSOLAP-compatible rowset string with proper XSD types.
    Column definitions from mdschema_config.json specify name, type, required,
    and maxOccurs — matching OlaPy's exact XSD output (confirmed working with Excel).
    """
    col_elements = ""
    for cdef in col_defs:
        name = cdef["name"]
        xsd_type = cdef.get("type", "string")
        required = cdef.get("required", False)
        max_occurs = cdef.get("maxOccurs", "")
        attrs = ""
        if max_occurs:
            attrs += f' maxOccurs="{max_occurs}"'
        if not required:
            attrs += ' minOccurs="0"'
        col_elements += f'<xs:element{attrs} name="{name}" sql:field="{name}" type="{_qualify_xsd_type(xsd_type)}"/>'

    schema = (
        f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:sql="{_SQL_NS}" xmlns="{_ROWSET_NS}" elementFormDefault="qualified" '
        f'targetNamespace="{_ROWSET_NS}">'
        f'<xs:element name="root"><xs:complexType>'
        f'<xs:sequence maxOccurs="unbounded" minOccurs="0">'
        f'<xs:element name="row" type="row"/>'
        f'</xs:sequence></xs:complexType></xs:element>'
        f'<xs:simpleType name="uuid"><xs:restriction base="xs:string">'
        f'<xs:pattern value="[0-9a-zA-Z]{{8}}-[0-9a-zA-Z]{{4}}-[0-9a-zA-Z]{{4}}-[0-9a-zA-Z]{{4}}-[0-9a-zA-Z]{{12}}"/>'
        f'</xs:restriction></xs:simpleType>'
        f'<xs:complexType name="xmlDocument"><xs:sequence><xs:any/></xs:sequence></xs:complexType>'
        f'<xs:complexType name="row"><xs:sequence>{col_elements}</xs:sequence></xs:complexType>'
        f'</xs:schema>'
    )

    # Only emit columns that are either present in the row data or REQUIRED.
    # Optional columns (minOccurs="0") can safely be absent from XML — MSOLAP
    # handles missing optional elements correctly. The original null-column crash
    # was caused by required columns being missing, not optional ones.
    # Emitting ALL columns (including optional uuid/boolean) with bad defaults
    # breaks MDSCHEMA_CUBES and other rowsets.
    _TYPE_DEFAULTS = {
        "string": "", "uuid": "00000000-0000-0000-0000-000000000000",
        "int": "0", "unsignedInt": "0", "unsignedShort": "0",
        "unsignedLong": "0", "short": "0", "boolean": "false",
        "dateTime": _meta_created(), "double": "0", "float": "0",
        "decimal": "0",
    }
    required_cols: set[str] = set()
    col_type_map: dict[str, str] = {}
    for cdef in col_defs:
        col_type_map[cdef["name"]] = cdef.get("type", "string")
        if cdef.get("required", False):
            required_cols.add(cdef["name"])

    col_names = [cdef["name"] for cdef in col_defs]
    rows_xml = ""
    for row_dict in rows:
        cells = ""
        for col in col_names:
            if col in row_dict:
                cells += f"<{col}>{_xe(str(row_dict[col]))}</{col}>"
            elif col in required_cols:
                # Required column missing from data — emit type-appropriate default
                default = _TYPE_DEFAULTS.get(col_type_map.get(col, "string"), "")
                cells += f"<{col}>{_xe(default)}</{col}>"
            # else: optional column not in data — skip it (safe for MSOLAP)
        rows_xml += f"<row>{cells}</row>"

    return (
        f'<return><root xmlns="{_ROWSET_NS}"'
        f' xmlns:xsd="http://www.w3.org/2001/XMLSchema"'
        f' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"{schema}{rows_xml}</root></return>"
    )

def _build_schema_rowsets_xml() -> str:
    """
    Build DISCOVER_SCHEMA_ROWSETS response matching OlaPy format exactly.
    OlaPy confirmed working with Excel — match its output byte-for-byte.

    Key OlaPy differences from previous Tessallite version:
    - GUIDs without braces
    - Restriction types use unqualified "string" not "xsd:string"
    - xs: prefix (not xsd:)
    - SchemaGuid type is "uuid" (custom simpleType)
    - RestrictionsMask type is "unsignedLong"
    - maxOccurs/minOccurs on xs:sequence not xs:element
    - Includes uuid simpleType and xmlDocument complexType
    """
    # Restrictions matching OlaPy's exact schema roster and restriction lists.
    # Types use unqualified names (no "xsd:" prefix) to match OlaPy format.
    _RESTRICTIONS: dict[str, list[tuple[str, str]]] = {
        "DBSCHEMA_TABLES": [
            ("TABLE_CATALOG", "string"), ("TABLE_SCHEMA", "string"),
            ("TABLE_NAME", "string"), ("TABLE_TYPE", "string"),
            ("TABLE_OLAP_TYPE", "string"),
        ],
        "DISCOVER_DATASOURCES": [
            ("DataSourceName", "string"), ("URL", "string"),
            ("ProviderName", "string"), ("ProviderType", "string"),
            ("AuthenticationMode", "string"),
        ],
        "DISCOVER_INSTANCES": [("INSTANCE_NAME", "string")],
        "DISCOVER_KEYWORDS": [("Keyword", "string")],
        "DBSCHEMA_CATALOGS": [("CATALOG_NAME", "string")],
        "DISCOVER_LITERALS": [("LiteralName", "string")],
        "DISCOVER_PROPERTIES": [("PropertyName", "string")],
        "DISCOVER_SCHEMA_ROWSETS": [("SchemaName", "string")],
        "DMSCHEMA_MINING_MODELS": [
            ("MODEL_CATALOG", "string"), ("MODEL_SCHEMA", "string"),
            ("MODEL_NAME", "string"), ("MODEL_TYPE", "string"),
            ("SERVICE_NAME", "string"), ("SERVICE_TYPE_ID", "unsignedInt"),
            ("MINING_STRUCTURE", "string"),
        ],
        "MDSCHEMA_ACTIONS": [
            ("CATALOG_NAME", "string"), ("SCHEMA_NAME", "string"),
            ("CUBE_NAME", "string"), ("ACTION_NAME", "string"),
            ("ACTION_TYPE", "int"), ("COORDINATE", "string"),
            ("COORDINATE_TYPE", "int"), ("INVOCATION", "int"),
            ("CUBE_SOURCE", "unsignedShort"),
        ],
        "MDSCHEMA_CUBES": [
            ("CATALOG_NAME", "string"), ("SCHEMA_NAME", "string"),
            ("CUBE_NAME", "string"), ("CUBE_SOURCE", "unsignedShort"),
            ("BASE_CUBE_NAME", "string"),
        ],
        "MDSCHEMA_DIMENSIONS": [
            ("CATALOG_NAME", "string"), ("SCHEMA_NAME", "string"),
            ("CUBE_NAME", "string"), ("DIMENSION_NAME", "string"),
            ("DIMENSION_UNIQUE_NAME", "string"), ("CUBE_SOURCE", "unsignedShort"),
            ("DIMENSION_VISIBILITY", "unsignedShort"),
        ],
        "MDSCHEMA_FUNCTIONS": [
            ("LIBRARY_NAME", "string"), ("INTERFACE_NAME", "string"),
            ("FUNCTION_NAME", "string"), ("ORIGIN", "int"),
        ],
        "MDSCHEMA_HIERARCHIES": [
            ("CATALOG_NAME", "string"), ("SCHEMA_NAME", "string"),
            ("CUBE_NAME", "string"), ("DIMENSION_UNIQUE_NAME", "string"),
            ("HIERARCHY_NAME", "string"), ("HIERARCHY_UNIQUE_NAME", "string"),
            ("HIERARCHY_ORIGIN", "unsignedShort"), ("CUBE_SOURCE", "unsignedShort"),
            ("HIERARCHY_VISIBILITY", "unsignedShort"),
        ],
        "MDSCHEMA_INPUT_DATASOURCES": [
            ("CATALOG_NAME", "string"), ("SCHEMA_NAME", "string"),
            ("DATASOURCE_NAME", "string"), ("DATASOURCE_TYPE", "string"),
        ],
        "MDSCHEMA_KPIS": [
            ("CATALOG_NAME", "string"), ("SCHEMA_NAME", "string"),
            ("CUBE_NAME", "string"), ("KPI_NAME", "string"),
            ("CUBE_SOURCE", "unsignedShort"),
        ],
        "MDSCHEMA_LEVELS": [
            ("CATALOG_NAME", "string"), ("SCHEMA_NAME", "string"),
            ("CUBE_NAME", "string"), ("DIMENSION_UNIQUE_NAME", "string"),
            ("HIERARCHY_UNIQUE_NAME", "string"), ("LEVEL_NAME", "string"),
            ("LEVEL_UNIQUE_NAME", "string"), ("LEVEL_ORIGIN", "unsignedShort"),
            ("CUBE_SOURCE", "unsignedShort"), ("LEVEL_VISIBILITY", "unsignedShort"),
        ],
        "MDSCHEMA_MEASUREGROUPS": [
            ("CATALOG_NAME", "string"), ("SCHEMA_NAME", "string"),
            ("CUBE_NAME", "string"), ("MEASUREGROUP_NAME", "string"),
        ],
        "MDSCHEMA_MEASUREGROUP_DIMENSIONS": [
            ("CATALOG_NAME", "string"), ("SCHEMA_NAME", "string"),
            ("CUBE_NAME", "string"), ("MEASUREGROUP_NAME", "string"),
            ("DIMENSION_UNIQUE_NAME", "string"), ("DIMENSION_VISIBILITY", "unsignedShort"),
        ],
        "MDSCHEMA_MEASURES": [
            ("CATALOG_NAME", "string"), ("SCHEMA_NAME", "string"),
            ("CUBE_NAME", "string"), ("MEASURE_NAME", "string"),
            ("MEASURE_UNIQUE_NAME", "string"), ("MEASUREGROUP_NAME", "string"),
            ("CUBE_SOURCE", "unsignedShort"), ("MEASURE_VISIBILITY", "unsignedShort"),
        ],
        "MDSCHEMA_MEMBERS": [
            ("CATALOG_NAME", "string"), ("SCHEMA_NAME", "string"),
            ("CUBE_NAME", "string"), ("DIMENSION_UNIQUE_NAME", "string"),
            ("HIERARCHY_UNIQUE_NAME", "string"), ("LEVEL_UNIQUE_NAME", "string"),
            ("LEVEL_NUMBER", "unsignedInt"), ("MEMBER_NAME", "string"),
            ("MEMBER_UNIQUE_NAME", "string"), ("MEMBER_CAPTION", "string"),
            ("MEMBER_TYPE", "int"), ("TREE_OP", "int"),
            ("CUBE_SOURCE", "unsignedShort"),
        ],
        "MDSCHEMA_PROPERTIES": [
            ("CATALOG_NAME", "string"), ("SCHEMA_NAME", "string"),
            ("CUBE_NAME", "string"), ("DIMENSION_UNIQUE_NAME", "string"),
            ("HIERARCHY_UNIQUE_NAME", "string"), ("LEVEL_UNIQUE_NAME", "string"),
            ("MEMBER_UNIQUE_NAME", "string"), ("PROPERTY_NAME", "string"),
            ("PROPERTY_TYPE", "string"), ("PROPERTY_CONTENT_TYPE", "string"),
            ("PROPERTY_ORIGIN", "unsignedShort"), ("CUBE_SOURCE", "unsignedShort"),
            ("PROPERTY_VISIBILITY", "unsignedShort"),
        ],
        "MDSCHEMA_SETS": [
            ("CATALOG_NAME", "string"), ("SCHEMA_NAME", "string"),
            ("CUBE_NAME", "string"), ("SET_NAME", "string"),
            ("SCOPE", "int"),
        ],
        # Bug-5430: Power BI / Tabular discovery rowsets advertised so a
        # discovering client knows the gateway dispatches them.
        "DISCOVER_CSDL_METADATA": [
            ("CATALOG_NAME", "string"), ("VERSION", "string"),
            ("PERSPECTIVE_NAME", "string"),
        ],
        "DISCOVER_CALC_DEPENDENCY": [
            ("DATABASE_NAME", "string"), ("OBJECT_TYPE", "string"),
            ("TABLE", "string"), ("OBJECT", "string"),
        ],
    }

    # Schema order matching OlaPy exactly (OlaPy starts with DBSCHEMA_TABLES).
    # The two Tabular rowsets (Bug-5430) are appended after the SSAS roster.
    _OLAPY_ORDER = [
        "DBSCHEMA_TABLES", "DISCOVER_DATASOURCES", "DISCOVER_INSTANCES",
        "DISCOVER_KEYWORDS", "DBSCHEMA_CATALOGS", "DISCOVER_LITERALS",
        "DISCOVER_PROPERTIES", "DISCOVER_SCHEMA_ROWSETS",
        "DMSCHEMA_MINING_MODELS", "MDSCHEMA_ACTIONS", "MDSCHEMA_CUBES",
        "MDSCHEMA_DIMENSIONS", "MDSCHEMA_FUNCTIONS", "MDSCHEMA_HIERARCHIES",
        "MDSCHEMA_INPUT_DATASOURCES", "MDSCHEMA_KPIS", "MDSCHEMA_LEVELS",
        "MDSCHEMA_MEASUREGROUPS", "MDSCHEMA_MEASUREGROUP_DIMENSIONS",
        "MDSCHEMA_MEASURES", "MDSCHEMA_MEMBERS", "MDSCHEMA_PROPERTIES",
        "MDSCHEMA_SETS",
        "DISCOVER_CSDL_METADATA", "DISCOVER_CALC_DEPENDENCY",
    ]

    rows_xml = ""
    for name in _OLAPY_ORDER:
        guid = _SCHEMA_GUIDS.get(name, "")
        restrictions = _RESTRICTIONS.get(name, [])
        mask = (1 << len(restrictions)) - 1 if restrictions else 0

        # Each restriction in its own <Restrictions> element (matches OlaPy)
        restriction_xml = ""
        for rname, rtype in restrictions:
            restriction_xml += (
                f"<Restrictions>"
                f"<Name>{_xe(rname)}</Name><Type>{_xe(rtype)}</Type>"
                f"</Restrictions>"
            )

        # OlaPy uses no braces around GUIDs
        guid_xml = guid if guid else ""
        rows_xml += (
            f"<row>"
            f"<SchemaName>{_xe(name)}</SchemaName>"
            f"<SchemaGuid>{guid_xml}</SchemaGuid>"
            f"{restriction_xml}"
            f"<RestrictionsMask>{mask}</RestrictionsMask>"
            f"</row>"
        )

    # XSD matching OlaPy format: uuid simpleType, xmlDocument complexType,
    # maxOccurs/minOccurs on sequence, SchemaGuid type=uuid, RestrictionsMask type=unsignedLong
    schema = (
        f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:sql="{_SQL_NS}" xmlns="{_ROWSET_NS}" elementFormDefault="qualified" '
        f'targetNamespace="{_ROWSET_NS}">'
        f'<xs:element name="root"><xs:complexType>'
        f'<xs:sequence maxOccurs="unbounded" minOccurs="0">'
        f'<xs:element name="row" type="row"/>'
        f'</xs:sequence></xs:complexType></xs:element>'
        f'<xs:simpleType name="uuid"><xs:restriction base="xs:string">'
        f'<xs:pattern value="[0-9a-zA-Z]{{8}}-[0-9a-zA-Z]{{4}}-[0-9a-zA-Z]{{4}}-[0-9a-zA-Z]{{4}}-[0-9a-zA-Z]{{12}}"/>'
        f'</xs:restriction></xs:simpleType>'
        f'<xs:complexType name="xmlDocument"><xs:sequence><xs:any/></xs:sequence></xs:complexType>'
        f'<xs:complexType name="row"><xs:sequence>'
        f'<xs:element minOccurs="0" name="SchemaName" sql:field="SchemaName" type="xs:string"/>'
        f'<xs:element minOccurs="0" name="SchemaGuid" sql:field="SchemaGuid" type="uuid"/>'
        f'<xs:element maxOccurs="unbounded" minOccurs="0" name="Restrictions" sql:field="Restrictions">'
        f'<xs:complexType><xs:sequence>'
        f'<xs:element minOccurs="0" name="Name" sql:field="Name" type="xs:string"/>'
        f'<xs:element minOccurs="0" name="Type" sql:field="Type" type="xs:string"/>'
        f'</xs:sequence></xs:complexType></xs:element>'
        f'<xs:element minOccurs="0" name="Description" sql:field="Description" type="xs:string"/>'
        f'<xs:element minOccurs="0" name="RestrictionsMask" sql:field="RestrictionsMask" type="xs:unsignedLong"/>'
        f'</xs:sequence></xs:complexType>'
        f'</xs:schema>'
    )

    return (
        f'<return><root xmlns="{_ROWSET_NS}"'
        f' xmlns:xsd="http://www.w3.org/2001/XMLSchema"'
        f' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"{schema}{rows_xml}</root></return>"
    )

def _build_trust_footer_xmla(trust_meta: dict[str, Any] | None) -> str:
    """Phase 5 of the semantic-layer plan: append freshness / source /
    owner to every XMLA description so Excel pivot field-list tooltips
    surface provenance without the user leaving the workbook.

    Returns "" when no signals are available so callers can append
    unconditionally without emitting an empty parenthetical.
    """
    if not trust_meta:
        return ""
    parts: list[str] = []
    last = trust_meta.get("last_refreshed_at")
    if last:
        cleaned = str(last).split(".")[0].replace("T", " ")
        parts.append(f"last refreshed {cleaned}")
    src = trust_meta.get("source_system")
    if src:
        parts.append(f"source: {src}")
    owner = trust_meta.get("owner")
    if owner:
        parts.append(f"owner: {owner}")
    return f"({', '.join(parts)})" if parts else ""


def _with_footer(description: str, footer: str) -> str:
    if not footer:
        return description or ""
    if not description:
        return footer
    return f"{description}\n{footer}"


def _get_rows(rtype, catalog, model_id, measures, dimensions, url, properties, restrictions, tenant_models, member_data, trust_meta, named_sets, kpis, named_queries):
    if rtype == "DISCOVER_DATASOURCES": return _rows_datasources(url)
    if rtype == "DISCOVER_PROPERTIES": return _rows_properties(restrictions, catalog)
    if rtype == "DISCOVER_LITERALS": return _rows_literals()
    if rtype in ("MDSCHEMA_CATALOGS", "DBSCHEMA_CATALOGS"): return _rows_catalogs(catalog, tenant_models)
    if rtype == "MDSCHEMA_CUBES": return _rows_cubes(catalog, tenant_models)
    if rtype == "MDSCHEMA_DIMENSIONS": return _rows_dimensions(catalog, dimensions, member_data, trust_meta)
    if rtype == "MDSCHEMA_MEASURES": return _rows_measures(catalog, measures, trust_meta)
    if rtype == "DBSCHEMA_TABLES": return _rows_tables(catalog, measures, dimensions, trust_meta, named_queries)
    if rtype == "DBSCHEMA_COLUMNS": return _rows_columns(catalog, measures, dimensions, trust_meta, named_queries)
    if rtype == "MDSCHEMA_HIERARCHIES": return _rows_hierarchies(catalog, dimensions, measures, member_data, properties, trust_meta)
    if rtype == "MDSCHEMA_LEVELS": return _rows_levels(catalog, dimensions, member_data, trust_meta)
    if rtype == "MDSCHEMA_MEASUREGROUPS": return _rows_measuregroups(catalog, measures)
    if rtype == "MDSCHEMA_MEASUREGROUP_DIMENSIONS": return _rows_measuregroup_dimensions(catalog, dimensions, measures)
    if rtype == "MDSCHEMA_MEMBERS": return _rows_members(catalog, measures, dimensions, restrictions, member_data)
    if rtype == "MDSCHEMA_PROPERTIES": return _rows_md_properties(catalog, dimensions, measures, restrictions)
    if rtype == "MDSCHEMA_SETS": return _rows_sets(catalog, named_sets)
    if rtype == "MDSCHEMA_KPIS": return _rows_kpis(catalog, kpis, measures)
    # Bug-5430: Power BI / Tabular discovery rowsets.
    if rtype == "DISCOVER_CSDL_METADATA": return _rows_csdl_metadata(catalog, measures, dimensions)
    if rtype == "DISCOVER_CALC_DEPENDENCY": return _rows_calc_dependency(catalog)
    return []

def _row(name: str, value: Any, access: str = "Read", ptype: str = "string") -> dict[str, str]:
    # OlaPy (working with Excel) uses PascalCase column names, not UPPER_CASE.
    # MSOLAP maps columns by name — wrong case = invisible to Excel.
    return {
        "PropertyName": name,
        "PropertyDescription": name,
        "PropertyType": ptype,
        "PropertyAccessType": access,
        "IsRequired": "false",
        "Value": str(value),
    }

def _rows_properties(restrictions: dict[str, list[str]], catalog: str = "") -> list[dict[str, str]]:
    # Properties matching OlaPy (confirmed working with Excel).
    # Catalog Value MUST echo the active catalog name — OlaPy does this.
    # Without it, Excel concludes the catalog doesn't exist.
    rows = [
        _row("ServerName", SERVER_NAME),
        _row("ProviderVersion", PROVIDER_VERSION),
        _row("MdpropMdxSubqueries", "15", ptype="int"),
        _row("MdpropMdxDrillFunctions", "3", ptype="int"),
        _row("MdpropMdxNamedSets", "15", ptype="int"),
        # Catalog MUST be ReadWrite with the current catalog echoed as Value.
        _row("Catalog", catalog, access="ReadWrite"),
        _row("Content", "SchemaData", access="ReadWrite"),
        _row("Format", "Tabular", access="ReadWrite"),
        _row("AxisFormat", "TupleFormat", access="ReadWrite"),
        _row("DataSourceInfo", "-", access="ReadWrite"),
    ]

    # Excel sends mixed-case tags in Restrictions (<PROPERTY_NAME> or <PropertyName>).
    name_filter = restrictions.get("PROPERTY_NAME") or restrictions.get("PropertyName")

    if name_filter:
        requested = set(name_filter)
        # 1. Keep templated rows that were requested
        filtered_rows = [row for row in rows if row["PropertyName"] in requested]

        # 2. Add stub rows for any requested names we didn't have a template for
        existing_names = {r["PropertyName"] for r in filtered_rows}
        for missing in requested - existing_names:
            filtered_rows.append(_row(missing, ""))

        return filtered_rows

    return rows


def _rows_datasources(url):
    """
    Return a datasource matching OlaPy's format (confirmed working with Excel).
    OlaPy column names: DataSourceName, DataSourceDescription, URL,
    DataSourceInfo, ProviderName, ProviderType, AuthenticationMode (PascalCase).
    """
    datasource_url = url or _datasource_url_fallback()
    return [{
        "DataSourceName": SERVER_NAME,
        "DataSourceDescription": "Tessallite Semantic Aggregation Layer",
        "URL": datasource_url,
        "DataSourceInfo": "-",
        "ProviderName": SERVER_NAME,
        "ProviderType": "MDP",
        "AuthenticationMode": "Authenticated",
    }]

def _rows_literals() -> list[dict[str, str]]:
    """DISCOVER_LITERALS — matching OlaPy output exactly (16 rows)."""
    def _lit(name, value="", invalid="", invalid_start="", max_len="-1", enum_val="0"):
        row = {"LiteralName": name, "LiteralMaxLength": max_len, "LiteralNameEnumValue": enum_val}
        if value: row["LiteralValue"] = value
        if invalid: row["LiteralInvalidChars"] = invalid
        if invalid_start: row["LiteralInvalidStartingChars"] = invalid_start
        return row
    return [
        _lit("DBLITERAL_CATALOG_NAME", invalid=".", invalid_start="0123456789", max_len="24", enum_val="2"),
        _lit("DBLITERAL_CATALOG_SEPARATOR", value=".", max_len="0", enum_val="3"),
        _lit("DBLITERAL_COLUMN_ALIAS", invalid="'\"[]", invalid_start="0123456789", enum_val="5"),
        _lit("DBLITERAL_COLUMN_NAME", invalid=".", invalid_start="0123456789", enum_val="6"),
        _lit("DBLITERAL_CORRELATION_NAME", invalid="'\"[]", invalid_start="0123456789", enum_val="7"),
        _lit("DBLITERAL_CUBE_NAME", invalid=".", invalid_start="0123456789", enum_val="21"),
        _lit("DBLITERAL_DIMENSION_NAME", invalid=".", invalid_start="0123456789", enum_val="22"),
        _lit("DBLITERAL_LEVEL_NAME", invalid=".", invalid_start="0123456789", enum_val="24"),
        _lit("DBLITERAL_MEMBER_NAME", invalid=".", invalid_start="0123456789", enum_val="25"),
        _lit("DBLITERAL_PROCEDURE_NAME", invalid=".", invalid_start="0123456789", enum_val="14"),
        _lit("DBLITERAL_PROPERTY_NAME", invalid=".", invalid_start="0123456789", enum_val="26"),
        _lit("DBLITERAL_QUOTE_PREFIX", value="[", enum_val="15"),
        _lit("DBLITERAL_QUOTE_SUFFIX", value="]", enum_val="28"),
        _lit("DBLITERAL_TABLE_NAME", invalid=".", invalid_start="0123456789", enum_val="17"),
        _lit("DBLITERAL_TEXT_COMMAND", enum_val="18"),
        _lit("DBLITERAL_USER_NAME", max_len="0", enum_val="19"),
    ]

def _rows_catalogs(catalog, tenant_models):
    """MDSCHEMA_CATALOGS — Phase 8 persona-as-catalog emits the base
    business catalog ``<slug>`` plus one ``<slug>_<persona.slug>``
    sibling per persona attached to the model. Callers must attach a
    ``personas`` list to each model dict (empty list is fine) before
    calling this function.

    Every row must carry the full column set declared in the
    MDSCHEMA_CATALOGS rowset config (CATALOG_NAME, DESCRIPTION, ROLES,
    DATE_MODIFIED, COMPATIBILITY_LEVEL, TYPE) — missing keys trip
    strict clients that inspect the schema before rendering.
    """
    def _row(cname: str, description: str) -> dict:
        return {
            "CATALOG_NAME": cname,
            "DESCRIPTION": description,
            "ROLES": "",
            "DATE_MODIFIED": _meta_created(),
            "COMPATIBILITY_LEVEL": "1600",
            "TYPE": "1",
        }

    if tenant_models:
        rows = []
        for m in tenant_models:
            base = m.get("slug") or m.get("display_name") or str(m.get("id", ""))
            display = m.get("display_name", "")
            variants: list[tuple[str, str]] = [("", "")]
            for persona in (m.get("personas") or []):
                pslug = persona.get("slug")
                if not pslug:
                    continue
                plabel = persona.get("description") or persona.get("name") or pslug
                variants.append((f"_{pslug}", f" ({plabel})"))
            for suffix, label_suffix in variants:
                cname = f"{base}{suffix}"
                if catalog and cname != catalog:
                    continue
                rows.append(_row(cname, f"{display}{label_suffix}".strip()))
        if rows:
            return rows
    if catalog:
        return [_row(catalog, "")]
    return []

def _rows_cubes(catalog, tenant_models=None):
    """MDSCHEMA_CUBES — one cube per catalog. Phase 8 persona-as-catalog
    exposes each model as the business base plus one cube per persona
    (``<slug>_<persona.slug>``). When the client requests the full cube
    list without a catalog restriction, every model × persona pair is
    emitted. Callers must attach a ``personas`` list to each model dict
    (empty list is fine).
    """
    def _cube_row(cat_name: str, description: str = "") -> dict:
        return {
            "CATALOG_NAME": cat_name,
            "SCHEMA_NAME": "",
            "CUBE_NAME": cat_name,
            "CUBE_TYPE": "CUBE",
            "CUBE_GUID": "00000000-0000-0000-0000-000000000000",
            "CREATED_ON": _meta_created(),
            "LAST_SCHEMA_UPDATE": _meta_modified(),
            "SCHEMA_UPDATED_BY": "",
            "LAST_DATA_UPDATE": _meta_modified(),
            "DATA_UPDATED_BY": "",
            "DESCRIPTION": description,
            "IS_DRILLTHROUGH_ENABLED": "true",
            "IS_LINKABLE": "false",
            "IS_WRITE_ENABLED": "false",
            "IS_SQL_ENABLED": "false",
            "CUBE_CAPTION": cat_name,
            "BASE_CUBE_NAME": cat_name,
            "CUBE_SOURCE": "1",
        }

    if not catalog:
        if not tenant_models:
            return []
        rows: list[dict] = []
        for m in tenant_models:
            base = m.get("slug") or m.get("display_name") or str(m.get("id", ""))
            display = m.get("display_name", "")
            variants: list[tuple[str, str]] = [("", "")]
            for persona in (m.get("personas") or []):
                pslug = persona.get("slug")
                if not pslug:
                    continue
                plabel = persona.get("description") or persona.get("name") or pslug
                variants.append((f"_{pslug}", f" ({plabel})"))
            for suffix, label in variants:
                rows.append(_cube_row(f"{base}{suffix}", f"{display}{label}".strip()))
        return rows
    return [_cube_row(catalog, "")]

def _effective_hidden(obj: dict) -> bool:
    """Compute whether a dimension or measure should be hidden from
    the end-user XMLA catalog.

    The gateway composes three signals into one decision:

    - ``is_hidden``: raw modeler toggle. Always wins when True.
    - ``is_invalid``: the Phase-1 structural validity flag. Invalid
      objects are hidden from the catalog so Excel can't pick them,
      but the query router still handles cached pivots via the
      source fallback + alert path.
    - ``redundant_partner``: the Phase-3 Rule C3 cascade. If a
      dimension / measure is a join-partner of a fact-side column,
      it is redundant to include in the catalog — hide it so the
      modeler (and Excel) sees only the canonical fact-side entry.

    Callers that need to distinguish these cases (e.g. the Model
    Health tab) should read the raw fields directly; the gateway
    only needs the composed answer.
    """
    if obj.get("is_hidden"):
        return True
    if obj.get("is_invalid"):
        return True
    if obj.get("redundant_partner"):
        return True
    return False


def _rows_dimensions(catalog, dims, member_data, trust_meta=None):
    """MDSCHEMA_DIMENSIONS — the field-list group nodes + Measures.

    Bug-6603 (field-list grouping): all STANDALONE attribute dimensions collapse
    into ONE ``[Dimensions]`` group node (mirroring the excel-plugin's single
    "Dimensions" section); each user/calendar hierarchy keeps its OWN node (its own
    group, which preserves its per-dimension time typing); KPIs are their own group
    natively via MDSCHEMA_KPIS. Grouping is by the DIMENSION_UNIQUE_NAME column only —
    the individual attribute hierarchies still appear under the group (emitted by
    ``_rows_hierarchies`` with the unchanged ``[Attr].[Attr]`` unique names), so a
    standalone dimension is NOT flattened.

    Phase 1: friendly ``display_name`` -> DIMENSION_CAPTION, business description ->
    DESCRIPTION, cascaded hidden dims dropped, visible dims marked visible.
    Phase 5: descriptions carry the trust footer.
    """
    rows = []
    footer = _build_trust_footer_xmla(trust_meta)
    visible_dims = [d for d in dims if not _effective_hidden(d)]
    standalone_dims = [d for d in visible_dims if is_standalone_attribute(d)]
    hierarchy_dims = [d for d in visible_dims if not is_standalone_attribute(d)]
    ordinal = 0

    # One group node for every standalone attribute dimension.
    if standalone_dims:
        first_name = standalone_dims[0].get("name", "")
        rows.append({
            "CATALOG_NAME": catalog,
            "SCHEMA_NAME": "",
            "CUBE_NAME": catalog,
            "DIMENSION_NAME": STANDALONE_GROUP_NAME,
            "DIMENSION_UNIQUE_NAME": STANDALONE_GROUP_UNIQUE_NAME,
            "DIMENSION_GUID": "00000000-0000-0000-0000-000000000000",
            "DIMENSION_CAPTION": STANDALONE_GROUP_NAME,
            "DIMENSION_ORDINAL": str(ordinal),
            # Attributes are not a single time dimension -> 3 (other). Time typing
            # lives on the calendar hierarchy nodes, which keep their own dimension.
            "DIMENSION_TYPE": "3",
            # Cardinality here is the count of grouped attribute hierarchies.
            "DIMENSION_CARDINALITY": str(len(standalone_dims)),
            "DEFAULT_HIERARCHY": f"[{_escape_mdx_bracket(first_name)}].[{_escape_mdx_bracket(first_name)}]",
            "DESCRIPTION": _with_footer("", footer),
            "IS_VIRTUAL": "false",
            "IS_READWRITE": "false",
            "DIMENSION_UNIQUE_SETTINGS": "1",
            "DIMENSION_MASTER_NAME": STANDALONE_GROUP_NAME,
            "DIMENSION_IS_VISIBLE": "true",
        })
        ordinal += 1

    # Bug-6891: multi-level user/calendar hierarchies collapse into ONE
    # [Hierarchies] group node; only flat time dimensions keep their own node
    # (preserving their per-dimension time typing / Excel timeline).
    grouped_hiers = [d for d in hierarchy_dims if is_grouped_hierarchy(d)]
    own_node_dims = [d for d in hierarchy_dims if not is_grouped_hierarchy(d)]

    if grouped_hiers:
        first_hname = grouped_hiers[0].get("name", "")
        rows.append({
            "CATALOG_NAME": catalog,
            "SCHEMA_NAME": "",
            "CUBE_NAME": catalog,
            "DIMENSION_NAME": HIERARCHY_GROUP_NAME,
            "DIMENSION_UNIQUE_NAME": HIERARCHY_GROUP_UNIQUE_NAME,
            "DIMENSION_GUID": "00000000-0000-0000-0000-000000000000",
            "DIMENSION_CAPTION": HIERARCHY_GROUP_NAME,
            "DIMENSION_ORDINAL": str(ordinal),
            # Mixed content (time and non-time hierarchies) -> 3 (other); level
            # time typing lives in MDSCHEMA_LEVELS.
            "DIMENSION_TYPE": "3",
            "DIMENSION_CARDINALITY": str(len(grouped_hiers)),
            "DEFAULT_HIERARCHY": f"[{_escape_mdx_bracket(first_hname)}].[{_escape_mdx_bracket(first_hname)}]",
            "DESCRIPTION": _with_footer("", footer),
            "IS_VIRTUAL": "false",
            "IS_READWRITE": "false",
            "DIMENSION_UNIQUE_SETTINGS": "1",
            "DIMENSION_MASTER_NAME": HIERARCHY_GROUP_NAME,
            "DIMENSION_IS_VISIBLE": "true",
        })
        ordinal += 1

    # Remaining dims (flat time dimensions) keep their own dimension node.
    for d in own_node_dims:
        dname = d.get("name", "")
        caption = d.get("display_name") or dname
        description = _with_footer(
            d.get("effective_description") or d.get("description") or "",
            footer,
        )
        dim_data = member_data.get(dname, {})
        card = str(len(dim_data.get("members", [])) or 23)
        dim_type = "1" if d.get("is_time_dim", False) else "3"
        rows.append({
            "CATALOG_NAME": catalog,
            "SCHEMA_NAME": "",
            "CUBE_NAME": catalog,
            "DIMENSION_NAME": dname,
            "DIMENSION_UNIQUE_NAME": dimension_unique_name_for(d),
            "DIMENSION_GUID": "00000000-0000-0000-0000-000000000000",
            "DIMENSION_CAPTION": caption,
            "DIMENSION_ORDINAL": str(ordinal),
            "DIMENSION_TYPE": dim_type,
            "DIMENSION_CARDINALITY": card,
            "DEFAULT_HIERARCHY": f"[{_escape_mdx_bracket(dname)}].[{_escape_mdx_bracket(dname)}]",
            "DESCRIPTION": description,
            "IS_VIRTUAL": "false",
            "IS_READWRITE": "false",
            "DIMENSION_UNIQUE_SETTINGS": "1",
            "DIMENSION_MASTER_NAME": dname,
            "DIMENSION_IS_VISIBLE": "true",
        })
        ordinal += 1

    # OlaPy also returns a [Measures] dimension
    rows.append({
        "CATALOG_NAME": catalog,
        "SCHEMA_NAME": "",
        "CUBE_NAME": catalog,
        "DIMENSION_NAME": "Measures",
        "DIMENSION_UNIQUE_NAME": "[Measures]",
        "DIMENSION_GUID": "00000000-0000-0000-0000-000000000000",
        "DIMENSION_CAPTION": "Measures",
        "DIMENSION_ORDINAL": str(ordinal),
        "DIMENSION_TYPE": "2",
        "DIMENSION_CARDINALITY": "0",
        "DEFAULT_HIERARCHY": "[Measures]",
        "DESCRIPTION": "",
        "IS_VIRTUAL": "false",
        "IS_READWRITE": "false",
        "DIMENSION_UNIQUE_SETTINGS": "1",
        "DIMENSION_MASTER_NAME": "Measures",
        "DIMENSION_IS_VISIBLE": "true",
    })
    return rows

_AGG_TO_XMLA = {
    "sum": "1",
    "count": "2",
    "min": "3",
    "max": "4",
    "avg": "5",
    "average": "5",
    "var": "6",
    "std": "7",
    "count_distinct": "127",
    "distinctcount": "127",
}

def _rows_measures(catalog, measures, trust_meta=None):
    """MDSCHEMA_MEASURES — one row per non-hidden measure.

    Phase 1 of the semantic-layer plan: emits friendly captions, business
    descriptions and display folders so Excel pivot field lists render
    measures inside expandable groups with helpful tooltips.
    Phase 5: each description is suffixed with the trust footer.
    """
    rows = []
    footer = _build_trust_footer_xmla(trust_meta)
    for m in measures:
        if _effective_hidden(m):
            continue
        mname = m.get("name", "")
        caption = m.get("display_name") or mname
        description = _with_footer(
            m.get("effective_description") or m.get("description") or "",
            footer,
        )
        folder = m.get("display_folder") or ""
        agg_code = _AGG_TO_XMLA.get((m.get("default_agg") or "sum").lower(), "1")
        # Bug-6889: the measure group carries the cube (model) name, matching
        # SSAS convention. A literal "default" surfaced as a meaningless
        # folder over every measure in Excel's field list.
        group_name = catalog
        rows.append({
            "CATALOG_NAME": catalog,
            "SCHEMA_NAME": "",
            "CUBE_NAME": catalog,
            "MEASURE_NAME": mname,
            "MEASURE_UNIQUE_NAME": f"[Measures].[{_escape_mdx_bracket(mname)}]",
            "MEASURE_CAPTION": caption,
            "MEASURE_GUID": "00000000-0000-0000-0000-000000000000",
            "MEASURE_AGGREGATOR": agg_code,
            "DATA_TYPE": "5",
            "NUMERIC_PRECISION": "16",
            "NUMERIC_SCALE": "-1",
            "MEASURE_UNITS": "",
            "DESCRIPTION": description,
            "EXPRESSION": "",
            # Bug-6888: KPI goal support measures exist for member resolution
            # only — invisible so they stay out of the visible field list.
            "MEASURE_IS_VISIBLE": (
                "false" if m.get("xmla_support_measure") else "true"
            ),
            "LEVELS_LIST": "",
            "MEASURE_NAME_SQL_COLUMN_NAME": mname,
            "MEASURE_UNQUALIFIED_CAPTION": caption,
            "MEASUREGROUP_NAME": group_name,
            "MEASURE_DISPLAY_FOLDER": folder,
            # Bug-5432: report the measure's SSAS/.NET FORMAT_STRING (translated
            # from the Tessallite format token) so Excel/Power BI format cells.
            "DEFAULT_FORMAT_STRING": format_token_to_mdx(m.get("format")) or "",
        })
    return rows

def _rows_tables(catalog, measures, dimensions, trust_meta=None, named_queries=None):
    """
    DBSCHEMA_TABLES — Excel sends this after selecting a database to list
    available tables. In SSAS, each cube appears as a table. We return the
    cube plus individual dimension tables and the Measures table.

    Phase 1 of the semantic-layer plan: hidden dimensions are skipped, and
    each row carries a DESCRIPTION populated from the dimension's business
    description.
    Phase 5: descriptions are suffixed with the trust footer.

    Bug-9178: each deployed Named Query is advertised as a first-class
    ``@name`` table (the same relation shape the JDBC catalogue registers),
    so Excel / Power BI table enumeration can see and reference it. The
    ``named_queries`` list is already persona-gated by the caller
    (``_handle_discover`` suppresses Named Queries on any surface where the
    persona narrows dimensions — Bug-9178 persona remediation), so this row
    builder advertises exactly what the caller passes; persona allow-lists /
    RLS / CLS on the Named Query's DATA are enforced by the query-router at
    query time, unchanged.
    """
    name = catalog
    now = _meta_modified()
    footer = _build_trust_footer_xmla(trust_meta)
    rows = [{
        "TABLE_CATALOG": name, "TABLE_NAME": name,
        "TABLE_TYPE": "TABLE",
        "DATE_CREATED": now, "DATE_MODIFIED": now,
        "DESCRIPTION": _with_footer("", footer),
    }]
    for d in dimensions:
        if _effective_hidden(d):
            continue
        dname = d.get("name", "")
        rows.append({
            "TABLE_CATALOG": name, "TABLE_NAME": dname,
            "TABLE_TYPE": "TABLE",
            "DATE_CREATED": now, "DATE_MODIFIED": now,
            "DESCRIPTION": _with_footer(
                d.get("effective_description") or d.get("description") or "",
                footer,
            ),
        })
    for nq in named_queries or []:
        if not isinstance(nq, dict):
            continue
        nq_name = str(nq.get("name") or "").strip()
        if not nq_name:
            continue
        rows.append({
            "TABLE_CATALOG": name, "TABLE_NAME": f"@{nq_name}",
            "TABLE_TYPE": "TABLE",
            "DATE_CREATED": now, "DATE_MODIFIED": now,
            "DESCRIPTION": _with_footer(
                nq.get("description")
                or f"Named Query @{nq_name} (deployed definition)",
                footer,
            ),
        })
    return rows


# Bug-9178: map the gateway catalogue data_type strings emitted by
# ``build_named_query_relation_columns`` to OLE DB DATA_TYPE codes for the
# DBSCHEMA_COLUMNS rowset. Codes match the sibling rows: measures -> 5
# (DBTYPE_R8), dimensions -> 130 (DBTYPE_WSTR).
_NQ_CATALOGUE_TO_OLE_DB_TYPE: dict[str, str] = {
    "float8": "5",     # DBTYPE_R8
    "bool": "11",      # DBTYPE_BOOL
    "date": "7",       # DBTYPE_DATE
    "timestamp": "135",  # DBTYPE_DBTIMESTAMP
}


def _rows_columns(catalog, measures, dimensions, trust_meta=None, named_queries=None):
    """DBSCHEMA_COLUMNS — columns within tables.

    Phase 1 of the semantic-layer plan: hidden measures and dimensions are
    skipped, and each column carries a DESCRIPTION populated from the
    semantic object's business description.
    Phase 5: descriptions are suffixed with the trust footer.

    Bug-9178: Named Query ``@name`` tables advertise their deployed
    ``output_columns`` (via ``build_named_query_relation_columns``, the same
    builder the JDBC catalogue uses) so the two channels agree on column
    names, ordinals and nullability.
    """
    name = catalog
    rows = []
    ordinal = 1
    footer = _build_trust_footer_xmla(trust_meta)
    for m in measures:
        if _effective_hidden(m):
            continue
        mname = m.get("name", "")
        rows.append({
            "TABLE_CATALOG": name, "TABLE_NAME": name,
            "COLUMN_NAME": mname, "ORDINAL_POSITION": str(ordinal),
            "IS_NULLABLE": "true", "DATA_TYPE": "5",
            "NUMERIC_PRECISION": "19", "NUMERIC_SCALE": "4",
            "DESCRIPTION": _with_footer(
                m.get("effective_description") or m.get("description") or "",
                footer,
            ),
        })
        ordinal += 1
    for d in dimensions:
        if _effective_hidden(d):
            continue
        dname = d.get("name", "")
        rows.append({
            "TABLE_CATALOG": name, "TABLE_NAME": dname,
            "COLUMN_NAME": dname, "ORDINAL_POSITION": "1",
            "IS_NULLABLE": "true", "DATA_TYPE": "130",
            "DESCRIPTION": d.get("effective_description") or d.get("description") or "",
        })
    for nq in named_queries or []:
        if not isinstance(nq, dict):
            continue
        nq_name = str(nq.get("name") or "").strip()
        if not nq_name:
            continue
        rel = f"@{nq_name}"
        for col in build_named_query_relation_columns(nq):
            col_name = str(col.get("name") or "").strip()
            if not col_name:
                continue
            data_type = _NQ_CATALOGUE_TO_OLE_DB_TYPE.get(
                str(col.get("data_type") or "text"), "130",
            )
            row: dict[str, str] = {
                "TABLE_CATALOG": name, "TABLE_NAME": rel,
                "COLUMN_NAME": col_name,
                "ORDINAL_POSITION": str(col.get("ordinal_position", 1)),
                "IS_NULLABLE": "true" if col.get("is_nullable") else "false",
                "DATA_TYPE": data_type,
                "DESCRIPTION": _with_footer(
                    str(col.get("description") or ""), footer,
                ),
            }
            if data_type == "5":
                # Mirror the measure rows' numeric precision/scale.
                row["NUMERIC_PRECISION"] = "19"
                row["NUMERIC_SCALE"] = "4"
            rows.append(row)
    return rows


def _dimension_level_names(dimension: dict, dim_data: dict | None = None) -> list[str]:
    levels = dimension.get("levels") or (dim_data or {}).get("levels") or []
    if levels:
        if isinstance(levels[0], dict):
            ordered = sorted(levels, key=lambda item: int(item.get("ordinal", 0)))
            names = [str(item.get("name", "")).strip() for item in ordered if str(item.get("name", "")).strip()]
            if names:
                return names
        else:
            names = [str(item).strip() for item in levels if str(item).strip()]
            if names:
                return names
    return [dimension.get("name", "")]


def _dimension_members_by_level(dim_data: dict | None) -> dict[int, list[dict]]:
    if not dim_data:
        return {}
    by_level_raw = dim_data.get("members_by_level")
    if isinstance(by_level_raw, dict):
        out: dict[int, list[dict]] = {}
        for key, members in by_level_raw.items():
            try:
                idx = int(key)
            except Exception:
                continue
            out[idx] = list(members or [])
        return out
    flat = list(dim_data.get("members") or [])
    if flat:
        return {0: flat}
    return {}


def _resolve_member_key_path(
    mem: dict,
    mname: str,
    parent_name: str,
    level_idx: int,
    members_by_level: dict[int, list[dict]],
    member_filter: str | None,
) -> list[str]:
    """Ancestor-first key path for a DISCOVER member (Bug-3617 Phase 2).

    Resolution priority:
      1. The explicit ``key_path`` the preview supplied (parent-less whole-level
         enumeration of a single-table hierarchy — Phase 0.5b).
      2. The chain walked up ``members_by_level`` via parent links (whole-hierarchy
         enumeration where all ancestor levels are loaded). Member names == keys in
         the discovery data, so the walked name path IS the key path.
      3. Drill case (only the target level loaded): reconstruct the parent path
         from the inbound canonical ``member_filter`` restriction — Excel echoes
         the parent's canonical uname when expanding children, so its key path is
         the ancestor prefix; ``self`` requests carry the member's own full path.
      4. Single-key fallback (root level, flat dimension, or a caption-only client).
    """
    explicit = mem.get("key_path")
    if explicit:
        return [str(k) for k in explicit]
    mem_key = str(mem.get("key") or mname)
    walked = ancestor_key_path_from_parent_chain(
        mname, level_idx, parent_name, members_by_level
    )
    if len(walked) >= level_idx + 1:
        return walked
    if member_filter:
        _h, _l, fgrammar, fpath = parse_member_uname(member_filter)
        if fgrammar == "key" and fpath:
            # filter is this member (self) -> its full path; else it is the parent
            # being expanded -> prefix the parent path onto this member's key(s).
            if walked and walked[-1] == fpath[-1]:
                return fpath
            return fpath + (walked or [mem_key])
    return walked or [mem_key]


def _member_name_from_unique(member_unique_name: str | None) -> str | None:
    """Deepest member key of a MEMBER_UNIQUE_NAME, via the single grammar parser.

    Bug-3617 (Phase 1): routed through ``parse_member_uname`` so a canonical
    name (``[Dim].[Hier].[Level].&[2025]&[4]``) yields the member key ``4`` — the
    old flat regex returned the LEVEL name (``Level``) for that shape. Caption
    form is unchanged (``[Dim].[Hier].[4]`` -> ``4``). Returns None for the
    (All)/Measures/invalid forms (each handled on its own path).
    """
    _hier, _level, grammar, key_path = parse_member_uname(member_unique_name)
    if grammar in ("invalid", "all", "measure") or not key_path:
        return None
    return key_path[-1]


def _rows_hierarchies(catalog, dimensions, measures=None, member_data=None, properties=None, trust_meta=None):
    """MDSCHEMA_HIERARCHIES — one hierarchy per dimension + Measures.
    Values match OlaPy: HIERARCHY_ORIGIN=1, DIMENSION_UNIQUE_SETTINGS=1,
    DEFAULT_MEMBER on Measures hierarchy. Phase 5 appends the trust
    footer to every DESCRIPTION."""
    if member_data is None:
        member_data = {}
    if properties is None:
        properties = {}
    name = catalog
    rows = []
    footer = _build_trust_footer_xmla(trust_meta)
    app_name = (properties.get("SspropInitAppName") or "").strip().lower()
    format_name = (properties.get("Format") or "").strip().upper()
    # Align with OlaPy's Excel-specific compatibility tweak from its filters
    # branch: avoid emitting ALL_MEMBER for Excel discover requests.
    include_all_member = format_name == "TABULAR" and "excel" not in app_name
    visible_dimensions = [d for d in dimensions if not _effective_hidden(d)]
    for i, d in enumerate(visible_dimensions):
        dname = d.get("name", "")
        caption = d.get("display_name") or dname
        description = _with_footer(
            d.get("effective_description") or d.get("description") or "",
            footer,
        )
        folder = d.get("display_folder") or ""
        dim_data = member_data.get(dname, {})
        hier = f"[{_escape_mdx_bracket(dname)}].[{_escape_mdx_bracket(dname)}]"
        members_by_level = _dimension_members_by_level(dim_data)
        # Count root-level members for cardinality when available.
        card = str(len(members_by_level.get(0, []))) if members_by_level.get(0) else "6"
        # All member — points to the (All) level member.
        # LEVEL_UNIQUE_NAME uses [(All)], MEMBER_UNIQUE_NAME uses [All] (SSAS convention).
        all_member = f"{hier}.[All]"
        dim_type = "1" if d.get("is_time_dim", False) else "3"
        row = {
            "CATALOG_NAME": name, "CUBE_NAME": name,
            # Bug-6603: group column — standalone attrs share [Dimensions]; the
            # HIERARCHY_UNIQUE_NAME below stays [dname].[dname] so Execute/member
            # discovery are unaffected.
            "DIMENSION_UNIQUE_NAME": dimension_unique_name_for(d),
            "HIERARCHY_NAME": dname,
            "HIERARCHY_UNIQUE_NAME": hier,
            "HIERARCHY_CAPTION": caption,
            "DIMENSION_TYPE": dim_type,
            "HIERARCHY_CARDINALITY": card,
            "DEFAULT_MEMBER": all_member,
            "DESCRIPTION": description,
            "STRUCTURE": "0",
            "IS_VIRTUAL": "false",
            "IS_READWRITE": "false",
            "DIMENSION_UNIQUE_SETTINGS": "1",
            "DIMENSION_IS_VISIBLE": "true",
            "HIERARCHY_ORDINAL": "1",
            "DIMENSION_IS_SHARED": "true",
            "HIERARCHY_IS_VISIBLE": "true",
            # Bug-6603: origin 1 (user-defined) only for multi-level model
            # hierarchies; flat single-column dimensions are attribute
            # hierarchies (origin 2) so Excel renders them as attribute fields,
            # not spurious one-level "user hierarchies" (Fable symptom 1).
            "HIERARCHY_ORIGIN": hierarchy_origin_for(d),
            "INSTANCE_SELECTION": "0",
            "HIERARCHY_DISPLAY_FOLDER": folder,
        }
        if include_all_member:
            row["ALL_MEMBER"] = all_member
        rows.append(row)
    # Measures hierarchy — DEFAULT_MEMBER pointing to first measure
    first_measure = ""
    if measures:
        first_measure = measures[0].get("name", "")
    default_member = f"[Measures].[{_escape_mdx_bracket(first_measure)}]" if first_measure else ""
    meas_row = {
        "CATALOG_NAME": name, "CUBE_NAME": name,
        "DIMENSION_UNIQUE_NAME": "[Measures]",
        "HIERARCHY_NAME": "Measures",
        "HIERARCHY_UNIQUE_NAME": "[Measures]",
        "HIERARCHY_CAPTION": "Measures",
        "DIMENSION_TYPE": "2",
        "HIERARCHY_CARDINALITY": "0",
        "DEFAULT_MEMBER": default_member,
        "STRUCTURE": "0",
        "IS_VIRTUAL": "false",
        "IS_READWRITE": "false",
        "DIMENSION_UNIQUE_SETTINGS": "1",
        "DIMENSION_IS_VISIBLE": "true",
        "HIERARCHY_ORDINAL": "1",
        "DIMENSION_IS_SHARED": "true",
        "HIERARCHY_IS_VISIBLE": "true",
        "HIERARCHY_ORIGIN": "1",
        "INSTANCE_SELECTION": "0",
    }
    rows.append(meas_row)
    return rows


_TIME_LEVEL_TYPES = {
    "year": "20", "years": "20",
    "half_year": "36", "half_years": "36", "semester": "36",
    "quarter": "68", "quarters": "68",
    "month": "132", "months": "132",
    "week": "516", "weeks": "516",
    "day": "1028", "days": "1028", "date": "1028",
}


def _time_level_type(level_name: str) -> str:
    """Map a time hierarchy level name to the XMLA MDLEVEL_TYPE constant."""
    return _TIME_LEVEL_TYPES.get(level_name.lower(), "0")


# Bug-6603: map a calendar level's authoritative ``time_unit`` (the model-service
# ``HierarchyLevel.time_unit``) to the XMLA MDLEVEL_TYPE. Preferred over the
# name heuristic so a calendar whose levels are renamed / localised still
# time-types correctly. Values mirror ``_TIME_LEVEL_TYPES``.
_TIME_UNIT_LEVEL_TYPES = {
    "year": "20",
    "half": "36", "half_year": "36", "semester": "36",
    "quarter": "68",
    "month": "132",
    "week": "516",
    "day": "1028",
}


def _level_time_type(level_name: str, time_unit: Any = None) -> str:
    """MDLEVEL_TYPE for a time level: authoritative ``time_unit`` first, then
    the level-name heuristic. ``hour``/``none``/unknown fall back to 0."""
    if time_unit:
        mapped = _TIME_UNIT_LEVEL_TYPES.get(str(time_unit).strip().lower())
        if mapped:
            return mapped
    return _time_level_type(level_name)


def _dimension_levels_detailed(dimension: dict, dim_data: dict | None = None) -> list[dict]:
    """Ordered ``[{name, time_unit}]`` for a dimension's data levels (Bug-6603).

    Mirrors :func:`_dimension_level_names` but preserves each level's
    ``time_unit`` so time hierarchies emit the correct MDLEVEL_TYPE. Falls
    back to a single self-named level (flat attribute dimension)."""
    levels = dimension.get("levels") or (dim_data or {}).get("levels") or []
    detailed: list[dict] = []
    if levels:
        if isinstance(levels[0], dict):
            ordered = sorted(levels, key=lambda item: int(item.get("ordinal", 0)))
            for item in ordered:
                name = str(item.get("name", "")).strip()
                if name:
                    detailed.append({"name": name, "time_unit": item.get("time_unit")})
        else:
            for item in levels:
                name = str(item).strip()
                if name:
                    detailed.append({"name": name, "time_unit": None})
    if detailed:
        return detailed
    return [{"name": dimension.get("name", ""), "time_unit": None}]


def _rows_levels(catalog, dimensions, member_data, trust_meta=None):
    """MDSCHEMA_LEVELS — levels per hierarchy + MeasuresLevel.
    Every hierarchy must have an (All) level at LEVEL_NUMBER=0 (LEVEL_TYPE=1)
    followed by the data level at LEVEL_NUMBER=1.  Without the (All) level
    MSOLAP rejects the cube schema and loops on MDSCHEMA_CUBES.

    Phase 1 of the semantic-layer plan: hidden dimensions are skipped, and
    each level carries the dimension's friendly caption and description.
    Phase 5: descriptions are suffixed with the trust footer.
    """
    name = catalog
    rows = []
    footer = _build_trust_footer_xmla(trust_meta)
    for d in dimensions:
        if _effective_hidden(d):
            continue
        dname = d.get("name", "")
        caption = d.get("display_name") or dname
        description = _with_footer(
            d.get("effective_description") or d.get("description") or "",
            footer,
        )
        dim_data = member_data.get(dname, {})
        members_by_level = _dimension_members_by_level(dim_data)
        # Bug-6603: carry each level's time_unit so calendar levels type
        # correctly even when the level names are not the canonical words.
        level_details = _dimension_levels_detailed(d, dim_data)
        hier = f"[{_escape_mdx_bracket(dname)}].[{_escape_mdx_bracket(dname)}]"
        # Bug-6603: group column (standalone attrs -> [Dimensions]); hierarchy/level
        # unique names below are unchanged.
        dim_uname = dimension_unique_name_for(d)

        # (All) level is always level 0 — MSOLAP requires it to match
        # the ALL_MEMBER / DEFAULT_MEMBER declared in MDSCHEMA_HIERARCHIES.
        rows.append({
            "CATALOG_NAME": name, "CUBE_NAME": name,
            "DIMENSION_UNIQUE_NAME": dim_uname,
            "HIERARCHY_UNIQUE_NAME": hier,
            "LEVEL_NAME": "(All)",
            "LEVEL_UNIQUE_NAME": f"{hier}.[(All)]",
            "LEVEL_CAPTION": "(All)",
            "LEVEL_NUMBER": "0",
            "LEVEL_CARDINALITY": "1",
            "LEVEL_TYPE": "1",  # MDLEVEL_TYPE_ALL
            "CUSTOM_ROLLUP_SETTINGS": "0",
            "LEVEL_UNIQUE_SETTINGS": "1",
            "LEVEL_IS_VISIBLE": "false",
            "LEVEL_DBTYPE": "130",
            "LEVEL_KEY_CARDINALITY": "1",
            "LEVEL_ORIGIN": "2",
            "DESCRIPTION": description,
        })

        # Regular hierarchy levels.
        is_time = d.get("is_time_dim", False)
        for idx, level in enumerate(level_details):
            level_name = level["name"]
            card = str(len(members_by_level.get(idx, [])))
            level_caption = caption if level_name == dname else level_name
            level_type = "0"
            if is_time:
                level_type = _level_time_type(level_name, level.get("time_unit"))
            rows.append({
                "CATALOG_NAME": name, "CUBE_NAME": name,
                "DIMENSION_UNIQUE_NAME": dim_uname,
                "HIERARCHY_UNIQUE_NAME": hier,
                "LEVEL_NAME": level_name,
                "LEVEL_UNIQUE_NAME": f"{hier}.[{_escape_mdx_bracket(level_name)}]",
                "LEVEL_CAPTION": level_caption,
                "LEVEL_NUMBER": str(idx + 1),
                "LEVEL_CARDINALITY": card,
                "LEVEL_TYPE": level_type,
                "CUSTOM_ROLLUP_SETTINGS": "0",
                "LEVEL_UNIQUE_SETTINGS": "0",
                "LEVEL_IS_VISIBLE": "true",
                "LEVEL_DBTYPE": "130",
                "LEVEL_KEY_CARDINALITY": "1",
                "LEVEL_ORIGIN": "2",
                "DESCRIPTION": description,
            })
    # Measures level
    rows.append({
        "CATALOG_NAME": name, "CUBE_NAME": name,
        "DIMENSION_UNIQUE_NAME": "[Measures]",
        "HIERARCHY_UNIQUE_NAME": "[Measures]",
        "LEVEL_NAME": "MeasuresLevel",
        "LEVEL_UNIQUE_NAME": "[Measures]",
        "LEVEL_CAPTION": "MeasuresLevel",
        "LEVEL_NUMBER": "0",
        "LEVEL_CARDINALITY": "0",
        "LEVEL_TYPE": "0",
        "CUSTOM_ROLLUP_SETTINGS": "0",
        "LEVEL_UNIQUE_SETTINGS": "0",
        "LEVEL_IS_VISIBLE": "true",
        "LEVEL_DBTYPE": "130",
        "LEVEL_KEY_CARDINALITY": "1",
        "LEVEL_ORIGIN": "2",
    })
    return rows


def _rows_members(
    catalog: str,
    measures: list[dict],
    dimensions: list[dict],
    restrictions: dict[str, list[str]],
    member_data: dict[str, dict] | None = None,
) -> list[dict]:
    """
    MDSCHEMA_MEMBERS — return members for dimensions/measures.
    Excel queries this for filter dropdowns and member validation.
    Uses member_data for real dimension members from the database.
    """
    if member_data is None:
        member_data = {}
    name = catalog
    # Normalize restriction keys to uppercase for case-insensitive matching
    _norm_restrictions: dict[str, list[str]] = {}
    for k, v in restrictions.items():
        _norm_restrictions[k.upper()] = v

    hier_filter = (_norm_restrictions.get("HIERARCHY_UNIQUE_NAME") or [None])[0]
    dim_filter = (_norm_restrictions.get("DIMENSION_UNIQUE_NAME") or [None])[0]
    level_filter = (_norm_restrictions.get("LEVEL_UNIQUE_NAME") or [None])[0]
    member_filter = (_norm_restrictions.get("MEMBER_UNIQUE_NAME") or [None])[0]
    tree_op = (_norm_restrictions.get("TREE_OP") or [None])[0]
    tree_op_int = int(tree_op) if tree_op else 0
    rows: list[dict] = []

    # If asking for children/descendants of a non-measure member, do NOT include measures
    skip_measures = False
    if member_filter and not member_filter.startswith("[Measures]"):
        skip_measures = True
    if hier_filter and hier_filter != "[Measures]":
        skip_measures = True
    if dim_filter and dim_filter != "[Measures]":
        skip_measures = True

    # Measure members — MEMBER_TYPE=3 (MDMEMBER_TYPE_MEASURE per SSAS spec)
    if not skip_measures:
        if not level_filter or level_filter == "[Measures]":
            for m in measures:
                mname = m.get("name", "")
                uname = f"[Measures].[{_escape_mdx_bracket(mname)}]"
                if member_filter and member_filter != uname:
                    # Measures don't have a parent-child tree — exact match only
                    continue
                rows.append({
                    "CATALOG_NAME": name,
                    "CUBE_NAME": name,
                    "DIMENSION_UNIQUE_NAME": "[Measures]",
                    "HIERARCHY_UNIQUE_NAME": "[Measures]",
                    "LEVEL_UNIQUE_NAME": "[Measures]",
                    "LEVEL_NUMBER": "0",
                    "MEMBER_ORDINAL": "0",
                    "MEMBER_NAME": mname,
                    "MEMBER_UNIQUE_NAME": uname,
                    "MEMBER_TYPE": "3",
                    "MEMBER_CAPTION": mname,
                    "CHILDREN_CARDINALITY": "0",
                    "PARENT_LEVEL": "0",
                    "PARENT_COUNT": "0",
                    "MEMBER_KEY": mname,
                    "IS_PLACEHOLDERMEMBER": "false",
                    "IS_DATAMEMBER": "false",
                })

    # Dimension members from member_data (real database values)
    for d in dimensions:
        # Bug-6603: hidden dims are absent from MDSCHEMA_DIMENSIONS, so they must not
        # emit member rows keyed to a DIMENSION_UNIQUE_NAME (incl. the [Dimensions]
        # group) that the DIMENSIONS rowset never advertised.
        if _effective_hidden(d):
            continue
        dname = d.get("name", "")
        # Bug-6603: DIMENSION_UNIQUE_NAME is the group column (standalone attrs ->
        # [Dimensions]); the hierarchy uname stays [dname].[dname]. A client that
        # restricts members by the group DIMENSION_UNIQUE_NAME keeps every standalone
        # dimension in scope; per-hierarchy narrowing still comes from hier_filter.
        dim_uname = dimension_unique_name_for(d)
        hier = f"[{_escape_mdx_bracket(dname)}].[{_escape_mdx_bracket(dname)}]"

        # Apply DIMENSION_UNIQUE_NAME restriction
        if dim_filter and dim_filter != dim_uname:
            continue
        # Apply HIERARCHY_UNIQUE_NAME restriction
        if hier_filter and hier_filter != hier:
            continue

        dim_data = member_data.get(dname) or {}
        level_names = _dimension_level_names(d, dim_data)
        members_by_level = _dimension_members_by_level(dim_data)
        root_members = members_by_level.get(0, [])
        all_level_uname = f"{hier}.[(All)]"
        all_member_uname = f"{hier}.[All]"
        # Bug-3617 (Phase 2): emit the canonical ancestor-qualified uname only for
        # MULTI-LEVEL hierarchies — that is where caption-form members collide
        # (month 4 of 2025 vs 2026) and where the Execute SUBTOTAL axis already
        # emits the canonical key form, so this restores DISCOVER<->Execute parity.
        # FLAT dimensions (one level) never collide and the non-subtotal Execute
        # axis keeps caption form, so they stay caption form here too — no mismatch.
        is_multi_level = len(level_names) > 1
        parsed_member_name = _member_name_from_unique(member_filter)
        # Bug-5431: full ancestor key path of the filter member (canonical input),
        # used for TREE_OP parent(2)/siblings(4)/ancestors(32) matching.
        _f_hier, _f_level, _f_grammar, filt_path = parse_member_uname(member_filter)

        # Emit the (All) member unless restrictions explicitly exclude it.
        emit_all = not level_filter or level_filter in (all_level_uname, all_member_uname)
        if member_filter:
            if member_filter == all_member_uname:
                # Excel uses TREE_OP to request self/children/descendants from [All].
                # Respect it here instead of always returning both the All member and
                # every child. Returning the wrong set confuses filter/unselect flows.
                if tree_op_int:
                    emit_all = bool(tree_op_int & 8)
            else:
                # A specific data member excludes the synthetic All member.
                emit_all = False

        if emit_all:
            rows.append({
                "CATALOG_NAME": name,
                "CUBE_NAME": name,
                "DIMENSION_UNIQUE_NAME": dim_uname,
                "HIERARCHY_UNIQUE_NAME": hier,
                "LEVEL_UNIQUE_NAME": all_level_uname,
                "LEVEL_NUMBER": "0",
                "MEMBER_ORDINAL": "0",
                "MEMBER_NAME": "All",
                "MEMBER_UNIQUE_NAME": all_member_uname,
                "MEMBER_TYPE": "2",  # MDMEMBER_TYPE_ALL
                "MEMBER_CAPTION": f"All {dname}",
                "CHILDREN_CARDINALITY": str(len(root_members)),
                "PARENT_LEVEL": "0",
                "PARENT_COUNT": "0",
                "MEMBER_KEY": "All",
                "IS_PLACEHOLDERMEMBER": "false",
                "IS_DATAMEMBER": "false",
            })

        for level_idx, level_name in enumerate(level_names):
            members = members_by_level.get(level_idx, [])
            if not members:
                continue
            level_uname = f"{hier}.[{_escape_mdx_bracket(level_name)}]"
            # Bug-5431: parent(2)/siblings(4)/ancestors(32) also span levels other
            # than level_filter, so don't filter them out by level here.
            _cross_level_tree_op = bool(
                tree_op_int & 1 or tree_op_int & 16 or tree_op_int & 2
                or tree_op_int & 4 or tree_op_int & 32
            )
            if (
                level_filter
                and not (member_filter and tree_op_int and _cross_level_tree_op)
                and level_filter != level_uname
            ):
                continue

            for mem in members:
                mname = str(mem.get("name", ""))
                parent_name = str(mem.get("parent") or "")
                # Bug-3617 (Phase 1/2): resolve the member's ancestor-first key
                # path ONCE, then use it for BOTH the dual-grammar matcher and the
                # canonical emit so DISCOVER and the matcher agree on identity.
                mem_caption = str(mem.get("caption") or mname)
                mem_key_path = _resolve_member_key_path(
                    mem, mname, parent_name, level_idx, members_by_level, member_filter,
                )
                mem_uname = (
                    qualify_member_uname(hier, level_name, mem_key_path)
                    if is_multi_level else f"{hier}.[{_escape_mdx_bracket(mname)}]"
                )
                matches_self = member_filter_matches(
                    member_filter,
                    candidate_hier_bracket=hier,
                    candidate_level_name=level_name,
                    candidate_key_path=mem_key_path,
                    candidate_caption=mem_caption,
                )

                if member_filter:
                    if tree_op_int:
                        want_self = bool(tree_op_int & 8)
                        want_children = bool(tree_op_int & 1)
                        want_descendants = bool(tree_op_int & 16)
                        want_parent = bool(tree_op_int & 2)
                        want_siblings = bool(tree_op_int & 4)
                        want_ancestors = bool(tree_op_int & 32)
                        include = False
                        if member_filter == all_member_uname:
                            if want_children and level_idx == 0:
                                include = True
                            elif want_descendants and level_idx >= 0:
                                include = True
                        else:
                            if want_self and matches_self:
                                include = True
                            elif (want_children or want_descendants) and parsed_member_name:
                                parent_name = str(mem.get("parent") or "")
                                if parent_name == parsed_member_name:
                                    include = True
                            # Bug-5431: parent / ancestors / siblings via the
                            # canonical filter key path (filt_path). Each compares
                            # this candidate's key path to the filter member's.
                            elif want_parent and filt_path and mem_key_path == list(filt_path[:-1]):
                                include = True
                            elif (
                                want_ancestors and filt_path
                                and 0 < len(mem_key_path) < len(filt_path)
                                and list(filt_path[: len(mem_key_path)]) == mem_key_path
                            ):
                                include = True
                            elif (
                                want_siblings and filt_path
                                and len(mem_key_path) == len(filt_path)
                                and mem_key_path[:-1] == list(filt_path[:-1])
                            ):
                                include = True
                        if not include:
                            continue
                    elif not matches_self:
                        continue

                next_level_members = members_by_level.get(level_idx + 1, [])
                child_count = sum(1 for m in next_level_members if str(m.get("parent") or "") == mname)

                # Bug-5434: flat (single-level) dimension with a distinct display
                # attribute. SSAS surfaces the display name as MEMBER_NAME /
                # MEMBER_CAPTION and the key separately as MEMBER_KEY; the member
                # is still identified by its KEY in MEMBER_UNIQUE_NAME. Previously
                # the flat branch emitted MEMBER_NAME = key, so a flat dim whose
                # member_data carried a separate caption surfaced
                # MEMBER_KEY == MEMBER_NAME == the raw key (caption lost on the
                # NAME axis). Multi-level hierarchies keep the Bug-3617 key-form
                # MEMBER_NAME the dual-grammar matcher depends on (unchanged).
                if is_multi_level:
                    member_name_out = mname
                else:
                    member_name_out = mem_caption

                # Bug-3617 (Phase 2): canonical SSAS member identity. MEMBER_UNIQUE_NAME
                # is the ancestor-qualified key path (built above) — byte-identical to
                # the SUBTOTAL Execute axis, so a client joining DISCOVER to Execute
                # sees ONE identity per member and month-4-of-2025 no longer collides
                # with month-4-of-2026. PARENT_UNIQUE_NAME is the same grammar (the
                # path minus the member's own key, at the parent level); MEMBER_CAPTION
                # is the display caption (may differ from the key); MEMBER_KEY is the
                # member's own deepest key. The dual-grammar matcher (Phase 1) still
                # accepts the legacy caption form a saved workbook may echo back.
                if is_multi_level:
                    parent_idx = len(mem_key_path) - 2
                    if 0 <= parent_idx < len(level_names):
                        parent_uname = qualify_member_uname(
                            hier, level_names[parent_idx], mem_key_path[:-1]
                        )
                    else:
                        parent_uname = all_member_uname
                else:
                    # Flat dimension: caption-form parent (original behaviour).
                    if level_idx == 0 or not parent_name:
                        parent_uname = all_member_uname
                    else:
                        parent_uname = f"{hier}.[{_escape_mdx_bracket(parent_name)}]"
                rows.append({
                    "CATALOG_NAME": name,
                    "CUBE_NAME": name,
                    "DIMENSION_UNIQUE_NAME": dim_uname,
                    "HIERARCHY_UNIQUE_NAME": hier,
                    "LEVEL_UNIQUE_NAME": level_uname,
                    "LEVEL_NUMBER": str(level_idx + 1),
                    "MEMBER_ORDINAL": str(mem.get("ordinal", 0)),
                    "MEMBER_NAME": member_name_out,
                    "MEMBER_UNIQUE_NAME": mem_uname,
                    "MEMBER_TYPE": "1",  # MDMEMBER_TYPE_REGULAR
                    "MEMBER_CAPTION": mem_caption,
                    "CHILDREN_CARDINALITY": str(child_count),
                    "PARENT_LEVEL": "0" if level_idx == 0 else str(level_idx),
                    "PARENT_UNIQUE_NAME": parent_uname,
                    "PARENT_COUNT": "0" if level_idx == 0 else "1",
                    "MEMBER_KEY": mem_key_path[-1] if mem_key_path else mname,
                    "IS_PLACEHOLDERMEMBER": "false",
                    "IS_DATAMEMBER": "false",
                })

    return rows


def _level_for_member(
    member_name: str,
    members: list[dict],
    levels: list[str],
    hier: str,
) -> str:
    """Return the level name for a given member from member data."""
    for mem in members:
        if mem["name"] == member_name:
            return mem.get("level", levels[0] if levels else "")
    return levels[0] if levels else ""


def _rows_measuregroups(catalog: str, measures: list[dict]) -> list[dict]:
    """MDSCHEMA_MEASUREGROUPS — all measures belong to one group named after
    the cube (model), matching SSAS convention (Bug-6889: a literal "default"
    group surfaced as a meaningless folder over every measure in Excel)."""
    return [
        {
            "CATALOG_NAME": catalog,
            "CUBE_NAME": catalog,
            "MEASUREGROUP_NAME": catalog,
            "DESCRIPTION": "-",
            "IS_WRITE_ENABLED": "true",
            "MEASUREGROUP_CAPTION": catalog,
        }
    ]


def _rows_measuregroup_dimensions(
    catalog: str,
    dimensions: list[dict],
    measures: list[dict],
) -> list[dict]:
    """MDSCHEMA_MEASUREGROUP_DIMENSIONS — one row per dimension NODE in the
    cube-named measure group (Bug-6889).

    Bug-6603: standalone attribute dims collapse into the single ``[Dimensions]``
    group node, so this rowset emits ONE row for that group (not one per collapsed
    attribute) plus one row per hierarchy node. Hidden dims are excluded so no row
    dangles a DIMENSION_UNIQUE_NAME that MDSCHEMA_DIMENSIONS never emitted.
    """
    rows: list[dict] = []
    seen_unames: set[str] = set()
    for d in dimensions:
        if _effective_hidden(d):
            continue
        dname = d.get("name", "")
        dim_uname = dimension_unique_name_for(d)
        if dim_uname in seen_unames:
            continue
        seen_unames.add(dim_uname)

        for gn in [catalog]:  # Bug-6889: group is named after the cube
            rows.append({
                "CATALOG_NAME": catalog,
                "CUBE_NAME": catalog,
                "MEASUREGROUP_NAME": gn,
                "MEASUREGROUP_CARDINALITY": "ONE",
                # Bug-6603: group column (standalone attrs -> [Dimensions]).
                "DIMENSION_UNIQUE_NAME": dim_uname,
                "DIMENSION_CARDINALITY": "MANY",
                "DIMENSION_IS_VISIBLE": "true",
                "DIMENSION_IS_FACT_DIMENSION": "false",
                "DIMENSION_GRANULARITY": f"[{_escape_mdx_bracket(dname)}].[{_escape_mdx_bracket(dname)}]",
            })
    return rows


def _xe(t): return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def _rows_md_properties(
    catalog: str,
    dimensions: list[dict],
    measures: list[dict],
    restrictions: dict[str, list[str]],
) -> list[dict]:
    """
    MDSCHEMA_PROPERTIES — cell and member properties.
    PROPERTY_TYPE values:
      1 = MDPROP_MEMBER (intrinsic member properties like KEY, ID)
      2 = MDPROP_CELL (cell properties like VALUE, FORMAT_STRING)
    Excel queries both types — we must return the correct type for each query.
    """
    name = catalog

    prop_type_filter = (restrictions.get("PROPERTY_TYPE") or [None])[0]
    hier_filter = (restrictions.get("HIERARCHY_UNIQUE_NAME") or [None])[0]
    prop_name_filter_vals = restrictions.get("PROPERTY_NAME") or restrictions.get("PropertyName") or []
    prop_name_filter = {v.strip().upper() for v in prop_name_filter_vals if v and v.strip()}

    member_props = [
        ("MEMBER_KEY", 130),
        ("MEMBER_VALUE", 130),
        ("MEMBER_NAME", 130),
        ("MEMBER_UNIQUE_NAME", 130),
        ("MEMBER_CAPTION", 130),
        ("LEVEL_UNIQUE_NAME", 130),
        ("LEVEL_NUMBER", 3),
        ("PARENT_UNIQUE_NAME", 130),
        ("HIERARCHY_UNIQUE_NAME", 130),
        ("MEMBER_TYPE", 3),
        ("MEMBER_ORDINAL", 19),
        ("CHILDREN_CARDINALITY", 19),
        ("DISPLAY_INFO", 19),
    ]
    cell_props = [
        ("VALUE", 130), ("FORMAT_STRING", 130), ("LANGUAGE", 19),
        ("BACK_COLOR", 19), ("FORE_COLOR", 19), ("FONT_FLAGS", 3),
        ("FONT_SIZE", 5), ("FONT_NAME", 130), ("FORMATTED_VALUE", 130),
        ("ACTION_TYPE", 3), ("CELL_ORDINAL", 19), ("UPDATEABLE", 11),
        ("STYLE", 130), ("className", 130),
    ]

    def _matches_name_filter(prop_name: str) -> bool:
        if not prop_name_filter:
            return True
        return prop_name.upper() in prop_name_filter

    def _member_property_rows() -> list[dict]:
        rows: list[dict] = []
        target_dimensions = []
        for d in dimensions:
            if _effective_hidden(d):
                continue
            dname = d.get("name", "")
            hier = f"[{_escape_mdx_bracket(dname)}].[{_escape_mdx_bracket(dname)}]"
            if hier_filter and hier_filter != hier:
                continue
            # Bug-6603: carry the group DIMENSION_UNIQUE_NAME (standalone attrs ->
            # [Dimensions]); the hierarchy uname stays [dname].[dname].
            target_dimensions.append(
                (dimension_unique_name_for(d), hier, _dimension_level_names(d))
            )

        if hier_filter and not target_dimensions:
            # Bug-6746: escape-aware bracket body so a hier_filter naming a
            # ]-containing dimension parses whole.
            dim_match = re.match(
                r"\[((?:[^\]]|\]\])+)\]\.\[(?:[^\]]|\]\])+\]", hier_filter,
            )
            if dim_match:
                # Raw name for the hidden-name comparison; keep the escaped body
                # for re-emission.
                dname_raw = unescape_member_key(dim_match.group(1))
                dname_escaped = dim_match.group(1)
                # A filter naming a HIDDEN dim must not resurrect it here: it is
                # absent from MDSCHEMA_DIMENSIONS, so emitting property rows for it
                # would dangle an unadvertised DIMENSION_UNIQUE_NAME (Bug-6603).
                hidden_names = {
                    d.get("name", "") for d in dimensions if _effective_hidden(d)
                }
                if dname_raw not in hidden_names:
                    # Unknown dimension (not in our list) — fall back to the
                    # hierarchy's own dimension bracket; grouping is unresolvable here.
                    target_dimensions.append(
                        (f"[{dname_escaped}]", hier_filter, [dname_raw])
                    )

        for dim_uname, hier, level_names in target_dimensions:
            level_unames = [f"{hier}.[(All)]"]
            level_unames.extend(
                f"{hier}.[{_escape_mdx_bracket(level_name)}]"
                for level_name in level_names
            )
            for level_uname in level_unames:
                for prop_name, data_type in member_props:
                    if not _matches_name_filter(prop_name):
                        continue
                    rows.append({
                        "CATALOG_NAME": name,
                        "CUBE_NAME": name,
                        "DIMENSION_UNIQUE_NAME": dim_uname,
                        "HIERARCHY_UNIQUE_NAME": hier,
                        "LEVEL_UNIQUE_NAME": level_uname,
                        "MEMBER_UNIQUE_NAME": "",
                        "PROPERTY_TYPE": "1",
                        "PROPERTY_NAME": prop_name,
                        "PROPERTY_CAPTION": prop_name,
                        "DATA_TYPE": str(data_type),
                        "DESCRIPTION": "",
                        "PROPERTY_CONTENT_TYPE": "0",
                        "PROPERTY_ORIGIN": "1",
                        "PROPERTY_IS_VISIBLE": "true",
                    })
        return rows

    def _cell_property_rows() -> list[dict]:
        rows: list[dict] = []
        for prop_name, data_type in cell_props:
            if not _matches_name_filter(prop_name):
                continue
            rows.append({
                "CATALOG_NAME": name,
                "CUBE_NAME": name,
                "DIMENSION_UNIQUE_NAME": "",
                "HIERARCHY_UNIQUE_NAME": "",
                "LEVEL_UNIQUE_NAME": "",
                "MEMBER_UNIQUE_NAME": "",
                "PROPERTY_TYPE": "2",
                "PROPERTY_NAME": prop_name,
                "PROPERTY_CAPTION": prop_name,
                "DATA_TYPE": str(data_type),
                "DESCRIPTION": "",
                "PROPERTY_CONTENT_TYPE": "0",
                "PROPERTY_ORIGIN": "1",
                "PROPERTY_IS_VISIBLE": "true",
            })
        return rows

    if prop_type_filter == "1":
        return _member_property_rows()
    if prop_type_filter == "2":
        return _cell_property_rows()
    return _member_property_rows() + _cell_property_rows()

def build_execute_response(catalog_name: str, columns: list[dict[str, Any]], rows: list[list[Any]]) -> str:
    col_defs = []
    for i, c in enumerate(columns):
        cname = c.get("name", f"Col{i}") if isinstance(c, dict) else str(c)
        col_defs.append({"name": cname, "type": "string", "required": False})

    dict_rows = []
    for r in rows:
        row_dict = {}
        for i, cdef in enumerate(col_defs):
            val = r[i] if i < len(r) else ""
            row_dict[cdef["name"]] = str(val) if val is not None else ""
        dict_rows.append(row_dict)

    return _build_rowset_xml(col_defs, dict_rows)


def _rows_sets(catalog: str, named_sets: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for ns in named_sets:
        # Bug-7925: sql_fixed named lists carry no MDX expression and
        # _inline_named_sets unconditionally skips them at execution time
        # (xmla_server.py:5215-5231).  Advertising them in MDSCHEMA_SETS
        # causes Excel to show a set that produces empty axes when used.
        # Filter them here at the discovery boundary so only MDX-executable
        # sets appear in the XMLA catalogue; sql_fixed lists remain
        # available through JDBC / REST / SQL authoring surfaces.
        if ns.get("list_type") == "sql_fixed":
            continue

        # F-018-13: deprecated sets are already filtered out at the gateway
        # client (router_client.get_model_named_sets). Surface the governance
        # state for the rest so BI users can tell a certified set from a draft:
        # certified/shared sets carry a "[Certified]" marker in the description.
        # Bug-6264 (authority: model-service named_sets.py:238-241): "shared" is
        # a certified-EQUIVALENT, admin-only status that must render the marker;
        # the gateway consumer MUST match the authority or the XMLA catalogue
        # desyncs from the model-service governance state.
        description = ns.get("description", "") or ""
        status = ns.get("certification_status")
        if status in ("certified", "shared"):
            marker = "[Certified] "
            description = (marker + description).strip()
        description = _with_footer(
            description, _build_trust_footer_xmla(ns.get("trust_meta")),
        )
        rows.append({
            "CATALOG_NAME": catalog,
            "SCHEMA_NAME": "",
            "CUBE_NAME": catalog,
            "SET_NAME": ns.get("name", ""),
            "SET_CAPTION": ns.get("display_name") or ns.get("name", ""),
            "SET_DESCRIPTION": description,
            "SET_DISPLAY_FOLDER": ns.get("display_folder", ""),
            "SCOPE": str(ns.get("scope", 1)),
            "EXPRESSION": ns.get("expression", ""),
            "DIMENSIONS": ns.get("dimensions", ""),
            "SET_EVALUATION_CONTEXT": "0",
        })
    return rows


# ---------------------------------------------------------------------------
# KPI band annotation (Bug-6608)
# ---------------------------------------------------------------------------


def _build_kpi_band_annotation(kpi: dict[str, Any]) -> str:
    """Human-readable band context published on MDSCHEMA_KPIS (informational).

    Bug-6608 (un-gated 2026-07-21): the KPI status is now the governed −1/0/1 RAG
    verdict served by the single model-service authority, and Excel renders the
    traffic-light graphic over it natively — no "colour it yourself" instruction is
    needed. This annotation is now purely informational metadata: it summarises the
    KPI's direction, evaluation type, and bands so a client that surfaces
    ANNOTATIONS / KPI_DESCRIPTION can show what the verdict is based on. Returns ""
    when the KPI defines no usable band context or carries an authored status
    expression (whose verdict is self-describing).

    Whether a BI client surfaces MDSCHEMA_KPIS ANNOTATIONS / KPI_DESCRIPTION is
    client-dependent — this is a best-effort metadata channel, not a guaranteed
    on-screen hint.
    """
    if str(kpi.get("status_expression") or "").strip():
        return ""

    pmeta = kpi.get("presentation_meta") or {}
    bands = pmeta.get("bands")
    band_dicts = (
        [b for b in bands if isinstance(b, dict)] if isinstance(bands, list) else []
    )
    direction = str(kpi.get("direction") or "").strip()
    if not band_dicts and not direction:
        return ""

    parts: list[str] = []
    if direction:
        parts.append(f"direction: {direction.replace('_', ' ')}")
    evaluation_type = str(pmeta.get("evaluation_type") or "").strip()
    if evaluation_type:
        parts.append(f"evaluation: {evaluation_type.replace('_', ' ')}")

    band_summaries: list[str] = []
    for b in band_dicts:
        lo = b.get("min")
        hi = b.get("max")
        label = str(b.get("label") or b.get("color") or "").strip()
        if lo is None and hi is None:
            rng = "any"
        elif lo is None:
            rng = f"< {hi}"
        elif hi is None:
            rng = f">= {lo}"
        else:
            rng = f"{lo} to {hi}"
        band_summaries.append(f"{rng}: {label}" if label else rng)

    text = "KPI status is the governed traffic-light verdict."
    if parts:
        text += " Based on — " + "; ".join(parts) + "."
    if band_summaries:
        text += " Bands: " + "; ".join(band_summaries) + "."
    return text


def resolve_kpi_goal_mdx(
    kpi: dict[str, Any],
    measure_map: dict[str, dict[str, Any]],
) -> str:
    """Resolve a KPI's goal/target to an EXECUTABLE MDX scalar, or ``""``.

    Single source of truth (used by MDSCHEMA_KPIS ``KPI_GOAL`` and the
    ``KPIGoal`` member-property path) so the two never drift.

    Bug-6259: a ``measure`` target is identified by ``target_measure_id`` and
    must render as ``[Measures].[<name>]``. The previous code read the (empty)
    ``target_expression`` for measure targets, producing an empty or
    non-executable goal. An ``expression`` (and ``prior_period``) target holds a
    Tessallite DSL string that is NOT executable MDX — emitting it verbatim
    advertised non-executable content to BI clients, so it is suppressed here.
    An empty goal then correctly skips status-expression construction
    (Bug-5695: no malformed CASE built against a blank goal).
    """
    target_type = kpi.get("target_type") or ""
    target_value = kpi.get("target_value")

    if target_type == "static" and target_value is not None:
        return str(target_value)

    if target_type == "measure":
        m = measure_map.get(str(kpi.get("target_measure_id", "")), {})
        name = (m.get("name", "") if m else "") or ""
        return f"[Measures].[{_escape_mdx_bracket(name)}]" if name else ""

    if target_type in ("expression", "prior_period"):
        # Tessallite DSL, not executable MDX -> do not advertise a goal.
        return ""

    # Legacy path: reference the goal measure by id.
    goal_m = measure_map.get(str(kpi.get("goal_measure_id", "")), {})
    name = (goal_m.get("name", "") if goal_m else "") or ""
    return f"[Measures].[{_escape_mdx_bracket(name)}]" if name else ""


def kpi_goal_support_measure_name(kpi: dict[str, Any]) -> str:
    """Name of the synthetic goal support measure for *kpi* (SSAS convention:
    ``<KPI caption> Goal``), or "" when the KPI has no caption."""
    base = kpi.get("display_name") or kpi.get("name", "") or ""
    return f"{base} Goal" if base else ""


def kpi_goal_needs_support_measure(kpi: dict[str, Any], measure_map: dict[str, dict[str, Any]]) -> bool:
    """True when the KPI's goal resolves to a bare scalar (static target).

    Bug-6888: Excel can only add pivot fields that are real members. A static
    target rendered as a bare literal in ``KPI_GOAL`` (e.g. ``188914000.0``)
    gave Excel nothing addable, so the KPI's Target checkbox was unusable. A
    measure-typed goal already resolves to ``[Measures].[...]`` and needs no
    synthetic member.
    """
    goal = resolve_kpi_goal_mdx(kpi, measure_map)
    return bool(goal) and not goal.startswith("[Measures].")


def kpi_goal_static_value(kpi: dict[str, Any], measure_map: dict[str, dict[str, Any]]) -> str:
    """The scalar goal value for a static-target KPI (as emitted string)."""
    goal = resolve_kpi_goal_mdx(kpi, measure_map)
    return "" if goal.startswith("[Measures].") else goal


def kpi_goal_synthetic_measures(
    kpis: list[dict[str, Any]], measures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Hidden support-measure dicts for static-goal KPIs (Bug-6888).

    Emitted into MDSCHEMA_MEASURES (with ``MEASURE_IS_VISIBLE=false`` via the
    ``xmla_support_measure`` marker) so the ``[Measures].[<KPI> Goal]`` member
    advertised in MDSCHEMA_KPIS exists as a resolvable measure for BI clients,
    without cluttering the visible field list.
    """
    measure_map = {str(m.get("id", "")): m for m in measures}
    # Bug-6942: skip any synthetic name that collides with a real measure.
    real_names = {(m.get("name") or "").lower() for m in measures if m.get("name")}
    out: list[dict[str, Any]] = []
    for kpi in kpis:
        if not kpi_goal_needs_support_measure(kpi, measure_map):
            continue
        name = kpi_goal_support_measure_name(kpi)
        if not name:
            continue
        if name.lower() in real_names:
            logger.warning(
                "Bug-6942: skipping synthetic KPI goal measure %r -- "
                "collides with a real measure of the same name.",
                name,
            )
            continue
        out.append({
            "name": name,
            "display_name": name,
            "description": f"Target for KPI {kpi.get('display_name') or kpi.get('name', '')}.",
            "display_folder": "",
            "default_agg": "max",
            "is_hidden": False,
            "xmla_support_measure": True,
        })
    return out


def kpi_status_support_measure_name(kpi: dict[str, Any]) -> str:
    """Name of the synthetic governed-status support member for *kpi* (SSAS
    convention: ``<KPI caption> Status``), or "" when the KPI has no caption."""
    base = kpi.get("display_name") or kpi.get("name", "") or ""
    return f"{base} Status" if base else ""


def kpi_status_needs_support_measure(
    kpi: dict[str, Any], measures: list[dict[str, Any]],
) -> bool:
    """True when a synthetic governed-status member should be advertised for *kpi*.

    Bug-8288: a native Excel pivot "Status" checkbox binds the MDSCHEMA_KPIS
    ``KPI_STATUS`` member DIRECTLY — its MDX carries no ``KPIStatus()`` token, so it
    bypasses the governed member-function interception and, when that member is the
    raw VALUE member, the pivot shows the raw business number instead of the
    governed −1/0/1 verdict. A synthetic ``[Measures].[<caption> Status]`` support
    member (Bug-6888 goal pattern) whose Execute resolves through the governed
    authority (``evaluate_kpi_governed``) fixes that. Advertise it only when:

      * the KPI has NO authored ``status_expression`` (an authored expression is
        already a real, addressable verdict member — keep serving it verbatim), AND
      * the KPI has a resolvable value member (a hidden-backed / unresolved value
        advertises no status member either), AND
      * the KPI has a verdict BASIS — a resolvable target/goal or presentation
        bands. Without a basis the governed status is always None (no verdict), so
        advertising a status member + graphic would be misleading noise; such a KPI
        keeps the raw value member (and no graphic), exactly as before.
    """
    if (kpi.get("status_expression") or ""):
        return False
    measure_map = {str(m.get("id", "")): m for m in measures}
    from src.dax.mdx_execute import resolve_kpi_property_expr
    try:
        if not resolve_kpi_property_expr(kpi, "KPIValue", measures):
            return False
    except ValueError:
        return False
    has_goal = bool(resolve_kpi_goal_mdx(kpi, measure_map))
    bands = (kpi.get("presentation_meta") or {}).get("bands") or []
    return has_goal or bool(bands)


def kpi_status_synthetic_measures(
    kpis: list[dict[str, Any]], measures: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Hidden support-measure dicts for governed-status KPIs (Bug-8288).

    Mirrors ``kpi_goal_synthetic_measures``: the ``[Measures].[<caption> Status]``
    member advertised in MDSCHEMA_KPIS must exist as a resolvable (hidden) measure
    row so a BI client can bind it; the XMLA Execute path resolves it to the
    governed −1/0/1 verdict through ``evaluate_kpi_governed`` (never a SQL column).
    A synthetic name that collides with a real measure is skipped (Bug-6942 parity)
    so the real measure's value is never hijacked.
    """
    real_names = {(m.get("name") or "").lower() for m in measures if m.get("name")}
    out: list[dict[str, Any]] = []
    for kpi in kpis:
        if not kpi_status_needs_support_measure(kpi, measures):
            continue
        name = kpi_status_support_measure_name(kpi)
        if not name:
            continue
        if name.lower() in real_names:
            logger.warning(
                "Bug-8288: skipping synthetic KPI status measure %r -- "
                "collides with a real measure of the same name.",
                name,
            )
            continue
        out.append({
            "name": name,
            "display_name": name,
            "description": (
                f"Governed RAG status for KPI "
                f"{kpi.get('display_name') or kpi.get('name', '')}."
            ),
            "display_folder": "",
            "default_agg": "max",
            "is_hidden": False,
            "xmla_support_measure": True,
        })
    return out


def _rows_kpis(
    catalog: str, kpis: list[dict[str, Any]], measures: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """MDSCHEMA_KPIS — one row per KPI.

    Supports both v2 expression-based KPIs and legacy measure-reference KPIs.
    For v2 KPIs the expression is emitted directly as KPI_VALUE. For legacy
    KPIs the value/goal measure names are wrapped in [Measures].[...] syntax.
    Composite KPIs populate KPI_PARENT_KPI_NAME for child KPIs.
    """
    measure_map = {str(m.get("id", "")): m for m in measures}
    # Build KPI name lookup for parent references
    kpi_name_map = {str(k.get("id", "")): k.get("name", "") for k in kpis}
    # Bug-6702: resolve KPI_VALUE through the SAME resolver the XMLA Execute path
    # uses (resolve_kpi_property_expr), against the SAME executable `measures`
    # set, so the advertised member and the Execute measure set can never
    # disagree. Imported lazily to avoid a module-load import cycle with
    # mdx_execute (which already imports resolve_kpi_goal_mdx from this module).
    from src.dax.mdx_execute import resolve_kpi_property_expr

    def _executable_kpi_value(kpi: dict[str, Any]) -> str:
        # value_measure_id -> [Measures].[<measure>]; a single-measure v2
        # expression (measure("X")) -> [Measures].[X]; anything else -> "".
        # The old F-017-23 synthetic `[Measures].[[KPI] <name>]` inline column is
        # NEVER present in the XMLA measure set (get_model_measures does not inject
        # it — inline columns are a JDBC-catalogue-only construct), so Execute
        # refused it with "Measure not available to this persona: [KPI ...".
        try:
            return resolve_kpi_property_expr(kpi, "KPIValue", measures) or ""
        except ValueError:
            return ""

    rows = []
    for kpi in kpis:
        kpi_name = kpi.get("name", "")
        kpi_value = _executable_kpi_value(kpi)

        # Goal / target (Bug-6259/Bug-5695): resolve to executable MDX or "".
        kpi_goal = resolve_kpi_goal_mdx(kpi, measure_map)
        # Bug-6888: a static target resolves to a bare scalar, which is not a
        # member and therefore not addable from Excel's KPI field list. Emit
        # the synthetic goal support member instead; the Execute path resolves
        # it to the constant, and MDSCHEMA_MEASURES carries a matching hidden
        # support-measure row (kpi_goal_synthetic_measures).
        if kpi_goal and not kpi_goal.startswith("[Measures]."):
            support_name = kpi_goal_support_measure_name(kpi)
            if support_name:
                kpi_goal = f"[Measures].[{_escape_mdx_bracket(support_name)}]"

        # Composite parent reference
        parent_id = kpi.get("parent_kpi_id")
        parent_name = kpi_name_map.get(str(parent_id), "") if parent_id else ""

        # Presentation type for status/trend graphics. Map each Tessallite
        # presentation type to the closest standard Excel KPI status graphic so
        # the metadata BI clients read agrees with what the UI renders
        # (Bug-5343: previously every non-gauge/bullet type defaulted to
        # "Traffic Light", advertising a graphic the UI did not draw).
        ptype = kpi.get("presentation_type") or ""
        status_graphic_map = {
            "traffic_light": "Traffic Light",
            "gauge": "Gauge",
            "reverse_gauge": "Gauge",
            "speedometer": "Gauge",
            "progress_ring": "Cylinder",
            "thermometer": "Thermometer",
            "bullet_chart": "Gauge",
            "rag_bar": "Shapes",
        }
        trend_graphic = "Standard Arrow"

        # Bug-6608 (un-gated 2026-07-21): the KPI live status path now serves the
        # governed −1/0/1 RAG verdict from the SINGLE model-service authority
        # (`kpi_threshold.evaluate_threshold`, same as the SPA scorecard and the
        # Excel custom function). The status is a real verdict again, so KPI_STATUS
        # advertises an addressable member (authored status expression when present,
        # else the value member) AND the status graphic is restored — the
        # report-builder traffic-light iconSet (calibrated for the −1/0/1 domain,
        # useExcel.ts `kpiIconCriteria`) resolves the governed status cell.
        legacy_status = kpi.get("status_expression") or ""
        legacy_trend = kpi.get("trend_expression") or ""
        # Bug-8288: with no authored status, advertise the synthetic governed
        # status support member ([Measures].[<caption> Status]) rather than the raw
        # value member, so a native pivot "Status" checkbox binds a member that the
        # Execute path resolves to the governed −1/0/1 verdict. Falls back to the
        # value member only when no support member can be built (name collision,
        # unresolved value). ``kpi_status_synthetic_measures`` emits the matching
        # hidden measure row into MDSCHEMA_MEASURES using the SAME predicate, so the
        # advertised member always exists as a resolvable measure.
        _status_support = kpi_status_support_measure_name(kpi)
        _real_lower = {(m.get("name") or "").lower() for m in measures if m.get("name")}
        use_synthetic_status = (
            not legacy_status
            and bool(_status_support)
            and kpi_status_needs_support_measure(kpi, measures)
            and _status_support.lower() not in _real_lower
        )
        if legacy_status:
            kpi_status = legacy_status
        elif use_synthetic_status:
            kpi_status = f"[Measures].[{_escape_mdx_bracket(_status_support)}]"
        else:
            kpi_status = kpi_value
        kpi_trend = legacy_trend
        band_annotation = _build_kpi_band_annotation(kpi)

        # Bug-8288 (was Bug-6608 un-gated / Fable R1 finding 1): MDSCHEMA_KPIS
        # KPI_STATUS is an ADDRESSABLE member a native OLAP client (Excel PivotTable
        # "Status" checkbox) binds DIRECTLY — its MDX carries no KPIStatus() token.
        # Previously that member was the raw VALUE member, so the graphic was KEPT
        # SUPPRESSED: a traffic-light icon over a raw business value would clamp a
        # business number onto the -1/0/1 icon domain (a "misleading verdict by
        # another name"). Now the member is the synthetic governed status member
        # (``[Measures].[<caption> Status]``), which the Execute path resolves to the
        # governed −1/0/1 verdict — a real verdict domain — so the graphic is
        # advertised again. It stays suppressed only when the status falls back to
        # the raw value member (no governed member could be built).
        status_graphic = (
            status_graphic_map.get(ptype, "Traffic Light")
            if (legacy_status or use_synthetic_status) else ""
        )

        kpi_description = kpi.get("description", "") or ""
        if band_annotation:
            kpi_description = (
                f"{kpi_description}\n{band_annotation}" if kpi_description
                else band_annotation
            )

        rows.append({
            "CATALOG_NAME": catalog,
            "SCHEMA_NAME": "",
            "CUBE_NAME": catalog,
            "MEASUREGROUP_NAME": catalog,  # Bug-6889: group named after the cube
            "KPI_NAME": kpi.get("name", ""),
            "KPI_CAPTION": kpi.get("display_name") or kpi.get("name", ""),
            "KPI_DESCRIPTION": kpi_description,
            "KPI_DISPLAY_FOLDER": kpi.get("display_folder", ""),
            "KPI_VALUE": kpi_value,
            "KPI_GOAL": kpi_goal,
            "KPI_STATUS": kpi_status,
            "KPI_TREND": kpi_trend,
            "KPI_STATUS_GRAPHIC": status_graphic,
            "KPI_TREND_GRAPHIC": trend_graphic,
            "KPI_WEIGHT": str(kpi.get("weight", "")) if kpi.get("weight") is not None else "",
            "KPI_CURRENT_TIME_MEMBER": "",
            "KPI_PARENT_KPI_NAME": parent_name,
            "ANNOTATIONS": band_annotation,
            "UNARY_OPERATOR": "",
            "ASSOCIATE_MEASURE_GROUP_NAME": catalog,
        })
    return rows


# ---------------------------------------------------------------------------
# Bug-5430: Power BI / Tabular (Analysis Services Tabular) discovery rowsets
# ---------------------------------------------------------------------------

def _rows_csdl_metadata(
    catalog: str,
    measures: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """DISCOVER_CSDL_METADATA — a single-row rowset whose ``Metadata`` cell
    carries a minimal CSDL (Conceptual Schema Definition Language) document
    describing the catalog as a Tabular model.

    Power BI Desktop and "Analyze in Excel" issue this against an XMLA endpoint
    to learn the Tabular shape (entity container, entity sets for each table,
    measure members). We are a semantic-aggregation layer, not a full Tabular
    server, so we emit a minimally-conformant CSDL envelope: one EntityType per
    dimension-bearing table plus the measure properties. This lets a Power BI
    discovery succeed (it gets a well-formed schema) instead of hard-failing on
    an unrecognised request type.

    The CSDL is embedded as an escaped XML string in the ``Metadata`` cell —
    this matches the SSAS contract, where the row column carries the document
    text rather than nested elements.
    """
    cube = catalog or "Model"
    props: list[str] = []
    for d in dimensions:
        dname = str(d.get("name", "")).strip()
        if not dname:
            continue
        props.append(
            f'<Property Name="{_xe(dname)}" Type="Edm.String" Nullable="true"/>'
        )
    for m in measures:
        mname = str(m.get("name", "")).strip()
        if not mname:
            continue
        props.append(
            f'<Property Name="{_xe(mname)}" Type="Edm.Double" Nullable="true"/>'
        )
    csdl = (
        '<edmx:Edmx Version="1.0" '
        'xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">'
        '<edmx:DataServices>'
        '<Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm" '
        f'Namespace="{_xe(cube)}">'
        f'<EntityType Name="{_xe(cube)}">'
        '<Key><PropertyRef Name="RowNumber"/></Key>'
        '<Property Name="RowNumber" Type="Edm.Int64" Nullable="false"/>'
        + "".join(props)
        + '</EntityType>'
        f'<EntityContainer Name="{_xe(cube)}">'
        f'<EntitySet Name="{_xe(cube)}" EntityType="{_xe(cube)}.{_xe(cube)}"/>'
        '</EntityContainer>'
        '</Schema>'
        '</edmx:DataServices>'
        '</edmx:Edmx>'
    )
    return [{"Metadata": csdl}]


def _rows_calc_dependency(catalog: str) -> list[dict[str, str]]:
    """DISCOVER_CALC_DEPENDENCY — calculation dependency graph for a Tabular
    model. Tessallite measures/dimensions are not Tabular calculation objects
    with a DAX dependency graph, so this rowset is legitimately EMPTY (the same
    way a Tabular model with no calculated columns/measures returns no rows).
    Returning the conformant empty rowset lets the discovery succeed rather than
    faulting on an unrecognised request type.
    """
    return []


# TMSCHEMA tables we surface from model metadata. A DMV referencing any other
# TMSCHEMA table gets the conformant empty-rowset answer.
TMSCHEMA_TABLES = {
    "TMSCHEMA_MODEL",
    "TMSCHEMA_TABLES",
    "TMSCHEMA_COLUMNS",
    "TMSCHEMA_MEASURES",
    "TMSCHEMA_HIERARCHIES",
    "TMSCHEMA_LEVELS",
    "TMSCHEMA_PARTITIONS",
    "TMSCHEMA_RELATIONSHIPS",
}


def build_tmschema_rowset(
    table: str,
    catalog: str,
    measures: list[dict[str, Any]],
    dimensions: list[dict[str, Any]],
    hierarchy_defs: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Build (col_defs, rows) for a ``$SYSTEM.TMSCHEMA_*`` DMV (Bug-5430).

    Returns a minimally-conformant Tabular-metadata projection built from the
    model's measures / dimensions / hierarchies. Unknown TMSCHEMA tables return
    an empty rowset with a single ``ID`` column so the response is well-formed.
    ``ID`` values are synthetic stable ordinals (1-based) — we have no Tabular
    object IDs, but Power BI only needs referential consistency within one DMV
    response, which the ordinals provide.
    """
    tbl = (table or "").upper()
    hierarchy_defs = hierarchy_defs or []
    cube = catalog or "Model"

    if tbl == "TMSCHEMA_MODEL":
        cols = [{"name": "ID", "type": "long"}, {"name": "Name", "type": "string"}]
        return cols, [{"ID": "1", "Name": cube}]

    if tbl == "TMSCHEMA_TABLES":
        cols = [
            {"name": "ID", "type": "long"}, {"name": "ModelID", "type": "long"},
            {"name": "Name", "type": "string"}, {"name": "IsHidden", "type": "boolean"},
        ]
        return cols, [{"ID": "1", "ModelID": "1", "Name": cube, "IsHidden": "false"}]

    if tbl == "TMSCHEMA_COLUMNS":
        cols = [
            {"name": "ID", "type": "long"}, {"name": "TableID", "type": "long"},
            {"name": "ExplicitName", "type": "string"},
            {"name": "DataType", "type": "int"}, {"name": "IsHidden", "type": "boolean"},
        ]
        rows: list[dict[str, str]] = []
        idx = 1
        for d in dimensions:
            dname = str(d.get("name", "")).strip()
            if not dname:
                continue
            rows.append({
                "ID": str(idx), "TableID": "1", "ExplicitName": dname,
                # DataType 2 = String in the TOM DataType enum.
                "DataType": "2",
                "IsHidden": "true" if d.get("is_hidden") else "false",
            })
            idx += 1
        return cols, rows

    if tbl == "TMSCHEMA_MEASURES":
        cols = [
            {"name": "ID", "type": "long"}, {"name": "TableID", "type": "long"},
            {"name": "Name", "type": "string"}, {"name": "Expression", "type": "string"},
            {"name": "IsHidden", "type": "boolean"},
        ]
        rows = []
        for idx, m in enumerate(measures, start=1):
            mname = str(m.get("name", "")).strip()
            if not mname:
                continue
            rows.append({
                "ID": str(idx), "TableID": "1", "Name": mname,
                "Expression": str(m.get("expression") or ""),
                "IsHidden": "true" if m.get("is_hidden") else "false",
            })
        return cols, rows

    if tbl == "TMSCHEMA_HIERARCHIES":
        cols = [
            {"name": "ID", "type": "long"}, {"name": "TableID", "type": "long"},
            {"name": "Name", "type": "string"}, {"name": "IsHidden", "type": "boolean"},
        ]
        rows = []
        for idx, h in enumerate(hierarchy_defs, start=1):
            hname = str(h.get("name", "")).strip()
            if not hname:
                continue
            rows.append({
                "ID": str(idx), "TableID": "1", "Name": hname, "IsHidden": "false",
            })
        return cols, rows

    if tbl == "TMSCHEMA_LEVELS":
        cols = [
            {"name": "ID", "type": "long"}, {"name": "HierarchyID", "type": "long"},
            {"name": "Ordinal", "type": "int"}, {"name": "Name", "type": "string"},
        ]
        rows = []
        lid = 1
        for hidx, h in enumerate(hierarchy_defs, start=1):
            levels = h.get("levels") or []
            for ordinal, lvl in enumerate(levels):
                lname = (
                    str(lvl.get("name", "")).strip()
                    if isinstance(lvl, dict) else str(lvl).strip()
                )
                if not lname:
                    continue
                rows.append({
                    "ID": str(lid), "HierarchyID": str(hidx),
                    "Ordinal": str(ordinal), "Name": lname,
                })
                lid += 1
        return cols, rows

    if tbl == "TMSCHEMA_PARTITIONS":
        cols = [
            {"name": "ID", "type": "long"}, {"name": "TableID", "type": "long"},
            {"name": "Name", "type": "string"},
        ]
        return cols, [{"ID": "1", "TableID": "1", "Name": f"{cube}-partition"}]

    # TMSCHEMA_RELATIONSHIPS and any other table: conformant empty rowset.
    return [{"name": "ID", "type": "long"}], []
