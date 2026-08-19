"""Tests for YAML serialiser/deserialiser roundtrip and diff."""
import copy
import uuid

import yaml

# The duplicate ``diff.py`` module was removed (F-020-10); ``differ`` is the
# single, production-wired snapshot diff (used by versions.py). These tests now
# exercise it directly.
from shared.model_snapshot.differ import diff_snapshots
from shared.model_snapshot.yaml_deserialiser import (
    YamlImportError,
    YamlSyntaxError,
    parse_model_yaml,
    parse_project_yaml,
)
from shared.model_snapshot.yaml_serialiser import project_to_yaml, snapshot_to_yaml


def _make_snapshot():
    """Build a snapshot using the REAL ``snapshot_model()`` field names
    (the ORM column names), not a fabricated shape.

    F-020-T1: the previous fixture hand-built keys (``name``/``table_name``/
    ``schema_name``/``table_role``/``hierarchy_type``) that the real
    serialiser never emits, so the round-trip test passed while both real
    directions were broken. This fixture mirrors ``_row_to_dict`` output:
    tables carry ``alias``/``physical_name``/``display_name``/``table_type``;
    hierarchies carry ``type``; levels carry ``key_attribute_id`` +
    ``key_attribute_source``.
    """
    model_id = str(uuid.uuid4())
    table_id = str(uuid.uuid4())
    col_amount_id = str(uuid.uuid4())
    col_name_id = str(uuid.uuid4())
    col_date_id = str(uuid.uuid4())

    return {
        "schema_version": 2,
        "model": {
            "id": model_id,
            "slug": "sales-model",
            "display_name": "Sales Model",
            "description": "Revenue analytics",
            "refresh_strategy": "scheduled",
            "max_aggregates": 20,
        },
        "tables": [
            {
                "id": table_id,
                "model_id": model_id,
                "source_id": str(uuid.uuid4()),
                "physical_name": "public.orders",
                "alias": "orders",
                "display_name": "Orders",
                "table_type": "fact",
                "description": "One row per order",
            },
        ],
        "columns": [
            {"id": col_amount_id, "model_table_id": table_id, "column_name": "amount_usd", "data_type": "numeric"},
            {
                "id": col_name_id,
                "model_table_id": table_id,
                "column_name": "customer_name",
                "data_type": "string",
                "is_primary_key": True,
            },
            {"id": col_date_id, "model_table_id": table_id, "column_name": "order_date", "data_type": "timestamp"},
        ],
        "joins": [],
        "dimensions": [
            {
                "id": str(uuid.uuid4()),
                "model_id": model_id,
                "name": "customer_name",
                "display_name": "Customer Name",
                "description": "Full name",
                "source_column_id": col_name_id,
                "is_time_dim": False,
            },
            {
                "id": str(uuid.uuid4()),
                "model_id": model_id,
                "name": "order_date",
                "display_name": "Order Date",
                "source_column_id": col_date_id,
                "is_time_dim": True,
            },
        ],
        "measures": [
            {
                "id": str(uuid.uuid4()),
                "model_id": model_id,
                "name": "total_revenue",
                "display_name": "Total Revenue",
                "description": "Sum of all order amounts",
                "source_column_id": col_amount_id,
                "default_agg": "sum",
                "measure_type": "standard",
                "format": "$#,##0.00",
                "display_folder": "Revenue",
            },
        ],
        "hierarchies": [
            {
                "id": str(uuid.uuid4()),
                "model_id": model_id,
                "name": "time",
                "type": "date_embedded",
                "calendar_type": "standard",
                "levels": [
                    {
                        "id": str(uuid.uuid4()), "name": "Year", "ordinal": 0,
                        "key_attribute_id": col_date_id,
                        "key_attribute_source": "physical_column",
                    },
                ],
            },
        ],
        "personas": [
            {
                "id": str(uuid.uuid4()),
                "model_id": model_id,
                "slug": "everyone",
                "name": "Everyone",
                "description": "Full access",
            },
        ],
        "aggregates": [],
        "data_sources": [],
        "data_targets": [],
    }


def _make_lossy_snapshot():
    """Fixture that exercises the THREE paths round-1 flagged as lossy
    (Bug-1100): a non-canonical (`right`) join cardinality, a UDA-keyed
    date-hierarchy level, and a variant measure that FKs to a base measure.

    Mirrors the real ``snapshot_model()`` shape: joins carry the raw ORM
    ``join_type``; hierarchies carry ``user_defined_attributes`` +
    ``uda_column_refs`` and levels keyed via ``key_attribute_source`` =
    ``user_defined_attribute``; variants carry ``variant_kind`` +
    ``variant_of_measure_id``.
    """
    model_id = str(uuid.uuid4())
    fact_id = str(uuid.uuid4())
    dim_id = str(uuid.uuid4())
    col_amount_id = str(uuid.uuid4())
    col_date_id = str(uuid.uuid4())
    col_fk_id = str(uuid.uuid4())
    col_dim_pk_id = str(uuid.uuid4())
    uda_year_id = str(uuid.uuid4())
    base_measure_id = str(uuid.uuid4())
    variant_measure_id = str(uuid.uuid4())

    return {
        "schema_version": 2,
        "model": {
            "id": model_id,
            "slug": "lossy-model",
            "display_name": "Lossy Model",
            "refresh_strategy": "manual",
            "max_aggregates": 20,
        },
        "tables": [
            {
                "id": fact_id, "model_id": model_id, "source_id": str(uuid.uuid4()),
                "physical_name": "public.sales", "alias": "sales",
                "display_name": "Sales", "table_type": "fact",
            },
            {
                "id": dim_id, "model_id": model_id, "source_id": str(uuid.uuid4()),
                "physical_name": "public.regions", "alias": "regions",
                "display_name": "Regions", "table_type": "dim_detail",
            },
        ],
        "columns": [
            {"id": col_amount_id, "model_table_id": fact_id, "column_name": "amount", "data_type": "numeric"},
            {"id": col_date_id, "model_table_id": fact_id, "column_name": "sale_date", "data_type": "timestamp"},
            {"id": col_fk_id, "model_table_id": fact_id, "column_name": "region_id", "data_type": "integer"},
            {"id": col_dim_pk_id, "model_table_id": dim_id, "column_name": "id", "data_type": "integer", "is_primary_key": True},
        ],
        "user_defined_attributes": [
            {
                "id": uda_year_id, "model_id": model_id, "table_id": fact_id,
                "name": "sale_year", "expression": "EXTRACT(YEAR FROM sale_date)",
                "output_data_type": "integer", "validated": True,
            },
        ],
        "uda_column_refs": [
            {"id": str(uuid.uuid4()), "attribute_id": uda_year_id, "column_id": col_date_id},
        ],
        "joins": [
            {
                "id": str(uuid.uuid4()), "model_id": model_id,
                "left_table_id": fact_id, "right_table_id": dim_id,
                # Non-canonical directional cardinality — must round-trip 1:1.
                "join_type": "right",
                "left_column_id": col_fk_id, "right_column_id": col_dim_pk_id,
            },
        ],
        "dimensions": [
            {
                "id": str(uuid.uuid4()), "model_id": model_id, "name": "sale_date",
                "source_column_id": col_date_id, "is_time_dim": True,
            },
        ],
        "measures": [
            {
                "id": base_measure_id, "model_id": model_id, "name": "revenue",
                "source_column_id": col_amount_id, "default_agg": "sum",
                "measure_type": "standard",
            },
            {
                "id": variant_measure_id, "model_id": model_id, "name": "revenue_ytd",
                "source_column_id": col_amount_id, "default_agg": "sum",
                "measure_type": "standard",
                "variant_kind": "ytd",
                "variant_of_measure_id": base_measure_id,
                "variant_n": 1,
            },
        ],
        "hierarchies": [
            {
                "id": str(uuid.uuid4()), "model_id": model_id,
                "name": "calendar", "type": "date_embedded", "calendar_type": "standard",
                "levels": [
                    {
                        "id": str(uuid.uuid4()), "name": "Year", "ordinal": 0,
                        "key_attribute_id": uda_year_id,
                        "key_attribute_source": "user_defined_attribute",
                        "time_unit": "year",
                    },
                    {
                        "id": str(uuid.uuid4()), "name": "Day", "ordinal": 1,
                        "key_attribute_id": col_date_id,
                        "key_attribute_source": "physical_column",
                    },
                ],
            },
        ],
        "personas": [],
        "aggregates": [],
        "data_sources": [],
        "data_targets": [],
    }


def test_join_orientation_roundtrips_non_canonical():
    """Bug-1097: a `right` join must survive export+import, not collapse to a
    default."""
    snap = _make_lossy_snapshot()
    yaml_str = snapshot_to_yaml(snap, connection_name="wh")
    doc = yaml.safe_load(yaml_str)
    # Export carries the raw orientation verbatim (not remapped, not dropped).
    assert doc["joins"][0]["type"] == "right"

    parsed = parse_model_yaml(yaml_str)
    assert len(parsed["joins"]) == 1
    assert parsed["joins"][0]["join_type"] == "right", (
        "a declared join orientation must round-trip 1:1, not default"
    )


def test_join_cardinality_roundtrips_as_its_own_field():
    """Cardinality is a SEPARATE exported key from the join type.

    Join orientation (which rows survive) and cardinality (how many rows on
    each side match) are two orthogonal properties of a join. Exporting only
    one of them silently loses the other, which is what happened while both
    shared a single ORM column.
    """
    snap = _make_lossy_snapshot()
    snap["joins"][0]["join_type"] = "left"
    snap["joins"][0]["cardinality"] = "one_to_many"
    yaml_str = snapshot_to_yaml(snap, connection_name="wh")
    doc = yaml.safe_load(yaml_str)
    assert doc["joins"][0]["type"] == "left"
    assert doc["joins"][0]["cardinality"] == "one-to-many"

    parsed = parse_model_yaml(yaml_str)
    assert parsed["joins"][0]["join_type"] == "left"
    assert parsed["joins"][0]["cardinality"] == "one_to_many"


def test_legacy_cardinality_in_type_is_split_not_dropped():
    """A file written BEFORE the split put the cardinality in ``type``.

    Importing it must (a) keep the cardinality rather than discard it, and
    (b) give the join a real orientation instead of leaving a cardinality
    token in the field that decides which rows survive. The inferred
    orientation is the one that preserves the cardinality label's many side,
    which reproduces the legacy rendering.
    """
    snap = _make_lossy_snapshot()
    snap["joins"][0]["join_type"] = "one_to_many"
    snap["joins"][0].pop("cardinality", None)
    yaml_str = snapshot_to_yaml(snap, connection_name="wh")
    doc = yaml.safe_load(yaml_str)
    assert doc["joins"][0]["type"] == "one-to-many"
    assert "cardinality" not in doc["joins"][0]

    parsed = parse_model_yaml(yaml_str)
    assert parsed["joins"][0]["cardinality"] == "one_to_many", (
        "the cardinality carried in the legacy ``type`` key was dropped"
    )
    assert parsed["joins"][0]["join_type"] == "right", (
        "one_to_many means the modeller's RIGHT table is the many side, so "
        "the orientation that preserves it is a RIGHT join"
    )


def test_population_participation_roundtrips_when_declared():
    """Bug-8615 G1: a deliberately declared population intent is model-defining
    content. Losing it on an export/import cycle would silently reset a
    ``population_defining`` join back to elidable, which is a wrong-numbers
    path once phase G3 reads the flag."""
    snap = _make_lossy_snapshot()
    snap["joins"][0]["population_participation"] = "population_defining"
    yaml_str = snapshot_to_yaml(snap, connection_name="wh")
    doc = yaml.safe_load(yaml_str)
    assert doc["joins"][0]["population_participation"] == "population_defining"

    parsed = parse_model_yaml(yaml_str)
    assert parsed["joins"][0]["population_participation"] == "population_defining"


def test_the_default_population_participation_is_not_written_to_yaml():
    """An untouched model's YAML must be byte-identical to what it was before
    this field existed, so a diff of an unrelated edit does not show a spurious
    join change."""
    snap = _make_lossy_snapshot()
    snap["joins"][0]["population_participation"] = "preserve_base_rows"
    doc = yaml.safe_load(snapshot_to_yaml(snap, connection_name="wh"))
    assert "population_participation" not in doc["joins"][0]


def test_a_yaml_file_without_the_key_imports_as_the_default():
    """Every file written before this field existed."""
    snap = _make_lossy_snapshot()
    snap["joins"][0].pop("population_participation", None)
    yaml_str = snapshot_to_yaml(snap, connection_name="wh")
    assert "population_participation" not in yaml_str
    parsed = parse_model_yaml(yaml_str)
    assert parsed["joins"][0]["population_participation"] == "preserve_base_rows"


def test_an_unknown_population_participation_imports_as_undeclared():
    """A hand-edited file must not fail the import, and must not be read as an
    affirmative declaration either."""
    snap = _make_lossy_snapshot()
    snap["joins"][0]["population_participation"] = "whatever_the_user_typed"
    parsed = parse_model_yaml(snapshot_to_yaml(snap, connection_name="wh"))
    assert parsed["joins"][0]["population_participation"] == "undeclared"


def test_hierarchy_uda_level_roundtrips():
    """Bug-1098: a UDA-keyed (derived) date-hierarchy level must survive
    export+import with its derivation, not be silently dropped."""
    snap = _make_lossy_snapshot()
    yaml_str = snapshot_to_yaml(snap, connection_name="wh")
    doc = yaml.safe_load(yaml_str)

    levels = doc["hierarchies"][0]["levels"]
    assert len(levels) == 2, "both the UDA level and the physical level must export"
    year = next(l for l in levels if l["name"] == "Year")
    assert year["derived"] is True
    assert year["from"] == "sales.sale_date"
    assert year["expression"] == "EXTRACT(YEAR FROM sale_date)"
    assert year["grain"] == "year"

    parsed = parse_model_yaml(yaml_str)
    hier = parsed["hierarchies"][0]
    # Both levels must reconstruct — the UDA level is no longer dropped.
    assert len(hier["levels"]) == 2
    yr_level = next(l for l in hier["levels"] if l["name"] == "Year")
    assert yr_level["key_attribute_source"] == "user_defined_attribute"
    assert yr_level["time_unit"] == "year"
    day_level = next(l for l in hier["levels"] if l["name"] == "Day")
    assert day_level["key_attribute_source"] == "physical_column"

    # The UDA itself must be reconstructed and linked to its source column.
    assert len(parsed["user_defined_attributes"]) == 1
    uda = parsed["user_defined_attributes"][0]
    assert uda["expression"] == "EXTRACT(YEAR FROM sale_date)"
    assert uda["output_data_type"] == "integer"
    assert uda["id"] == yr_level["key_attribute_id"]
    assert len(parsed["uda_column_refs"]) == 1
    ref = parsed["uda_column_refs"][0]
    assert ref["attribute_id"] == uda["id"]
    # The UDA column ref points at the sale_date column.
    sale_date_col = next(
        c for c in parsed["columns"] if c["column_name"] == "sale_date"
    )
    assert ref["column_id"] == sale_date_col["id"]


def test_variant_measure_roundtrips():
    """Bug-1099: a variant measure must survive export+import linked to its
    base measure by name, not be skipped."""
    snap = _make_lossy_snapshot()
    yaml_str = snapshot_to_yaml(snap, connection_name="wh")
    doc = yaml.safe_load(yaml_str)

    variant = next(m for m in doc["measures"] if m["name"] == "revenue_ytd")
    assert variant["variant"] == "ytd"
    assert variant["variant_of"] == "revenue"

    parsed = parse_model_yaml(yaml_str)
    # Both base and variant survive (no 19->18 silent loss).
    assert len(parsed["measures"]) == 2
    base = next(m for m in parsed["measures"] if m["name"] == "revenue")
    var = next(m for m in parsed["measures"] if m["name"] == "revenue_ytd")
    assert var["variant_kind"] == "ytd"
    assert var["variant_n"] == 1
    # The base linkage resolves by name to the base measure's new id.
    assert var["variant_of_measure_id"] == base["id"]
    assert "variant_kind" not in base or base.get("variant_kind") is None


def test_lossy_snapshot_emits_no_warnings_on_reimport():
    """The three previously-lossy paths must now round-trip cleanly — the
    deserialiser should not warn that it dropped them."""
    snap = _make_lossy_snapshot()
    yaml_str = snapshot_to_yaml(snap, connection_name="wh")
    parsed = parse_model_yaml(yaml_str)
    warnings = parsed.get("warnings", [])
    joined = " ".join(warnings).lower()
    assert "skipped" not in joined, f"unexpected loss warnings: {warnings}"


def test_snapshot_to_yaml_produces_valid_yaml():
    snap = _make_snapshot()
    result = snapshot_to_yaml(snap, connection_name="main-warehouse")
    doc = yaml.safe_load(result)
    assert doc["model"]["name"] == "sales-model"
    assert doc["model"]["display_name"] == "Sales Model"
    assert doc["model"]["connection"] == "main-warehouse"
    # F-020-03: real snapshot tables export with their alias as name and
    # physical_name as source_table (previously both came out empty).
    assert len(doc["tables"]) == 1
    assert doc["tables"][0]["name"] == "orders"
    assert doc["tables"][0]["source_table"] == "public.orders"
    assert doc["tables"][0]["role"] == "fact"
    assert len(doc["measures"]) == 1
    assert doc["measures"][0]["name"] == "total_revenue"
    assert doc["measures"][0]["aggregation"] == "sum"
    assert doc["measures"][0]["table"] == "orders"
    assert doc["measures"][0]["column"] == "amount_usd"
    assert len(doc["dimensions"]) == 2
    customer = next(dim for dim in doc["dimensions"] if dim["name"] == "customer_name")
    assert customer["primary_key"] is True
    assert len(doc["hierarchies"]) == 1
    # F-020-03: real ORM column is `type`; a date hierarchy must not flatten
    # to `explicit`.
    assert doc["hierarchies"][0]["type"] == "date"
    assert doc["hierarchies"][0]["calendar"] == "standard"
    # The date level keyed on order_date round-trips as a table.column ref.
    assert doc["hierarchies"][0]["levels"][0]["column"] == "orders.order_date"
    assert len(doc["personas"]) == 1


def test_yaml_roundtrip_preserves_model_identity():
    snap = _make_snapshot()
    yaml_str = snapshot_to_yaml(snap, connection_name="main-warehouse")
    reimported = parse_model_yaml(yaml_str)
    assert reimported["model"]["slug"] == "sales-model"
    assert reimported["model"]["display_name"] == "Sales Model"
    assert len(reimported["measures"]) == 1
    assert reimported["measures"][0]["name"] == "total_revenue"
    assert reimported["measures"][0]["default_agg"] == "sum"
    assert len(reimported["dimensions"]) == 2
    assert len(reimported["hierarchies"]) == 1
    customer_dimension = next(
        dim for dim in reimported["dimensions"] if dim["name"] == "customer_name"
    )
    customer_column = next(
        column for column in reimported["columns"]
        if column["id"] == customer_dimension["source_column_id"]
    )
    assert customer_column["is_primary_key"] is True


def test_yaml_import_emits_orm_valid_field_names():
    """F-020-02: the deserialiser output must use real ORM column names and
    supply every NOT NULL column, so rehydrate_into_live's insert() calls
    don't raise 'Unconsumed column names'."""
    snap = _make_snapshot()
    yaml_str = snapshot_to_yaml(snap, connection_name="main-warehouse")
    parsed = parse_model_yaml(yaml_str)

    # A placeholder data source must exist (ModelTable.source_id is NOT NULL).
    assert len(parsed["data_sources"]) == 1
    source_id = parsed["data_sources"][0]["id"]
    assert parsed["data_sources"][0]["source_type"] == "import_placeholder"

    # Tables: real ModelTable NOT NULL columns, no fabricated keys.
    table = parsed["tables"][0]
    for required in ("source_id", "table_type", "physical_name", "alias", "display_name"):
        assert required in table, f"table missing ORM column {required}"
    assert table["source_id"] == source_id
    assert table["alias"] == "orders"
    assert table["physical_name"] == "public.orders"
    for forbidden in ("name", "table_name", "schema_name", "source_table", "table_role"):
        assert forbidden not in table, f"table carries non-ORM key {forbidden}"

    # Hierarchy: ORM column `type`, not `hierarchy_type`; levels carry the
    # NOT NULL key_attribute_id + key_attribute_source.
    hier = parsed["hierarchies"][0]
    assert "type" in hier and "hierarchy_type" not in hier
    assert hier["type"] == "date_embedded"
    level = hier["levels"][0]
    assert level["key_attribute_source"] == "physical_column"
    assert level["key_attribute_id"]
    assert "display_name" not in level  # not an ORM column

    # Persona: NOT NULL name present; no non-ORM display_name.
    persona = parsed["personas"][0]
    assert persona["name"]
    assert "display_name" not in persona


def test_yaml_import_join_emits_column_ids():
    """F-020-02: joins must carry left_column_id / right_column_id (NOT NULL
    Join FKs), not the old left_column / right_column / on_expression keys."""
    model_id = str(uuid.uuid4())
    snap = _make_snapshot()
    snap["model"]["id"] = model_id
    # Add a second table and a join so the export carries an `on` condition.
    cust_table = str(uuid.uuid4())
    cust_id_col = str(uuid.uuid4())
    fk_col = str(uuid.uuid4())
    src = snap["tables"][0]["source_id"]
    snap["tables"].append({
        "id": cust_table, "model_id": snap["model"]["id"], "source_id": src,
        "physical_name": "public.customers", "alias": "customers",
        "display_name": "Customers", "table_type": "dim_detail",
    })
    fact_id = snap["tables"][0]["id"]
    snap["columns"].append({"id": fk_col, "model_table_id": fact_id, "column_name": "customer_id", "data_type": "integer"})
    snap["columns"].append({"id": cust_id_col, "model_table_id": cust_table, "column_name": "id", "data_type": "integer", "is_primary_key": True})
    snap["joins"] = [{
        "id": str(uuid.uuid4()), "model_id": snap["model"]["id"],
        "left_table_id": fact_id, "right_table_id": cust_table,
        "join_type": "many_to_one",
        "left_column_id": fk_col, "right_column_id": cust_id_col,
    }]

    yaml_str = snapshot_to_yaml(snap, connection_name="wh")
    parsed = parse_model_yaml(yaml_str)
    assert len(parsed["joins"]) == 1
    join = parsed["joins"][0]
    assert join["left_column_id"]
    assert join["right_column_id"]
    for forbidden in ("left_column", "right_column", "on_expression"):
        assert forbidden not in join


def test_parse_model_yaml_validates_required_fields():
    try:
        parse_model_yaml("not_a_model: true")
        assert False, "Should have raised"
    except YamlImportError as e:
        assert "model" in e.errors[0].lower()


# ---------------------------------------------------------------------------
# Bug-8139: a genuinely malformed YAML document (not a well-formed document
# missing a field -- an unparseable one) must raise YamlSyntaxError with
# line/column, not propagate a raw yaml.YAMLError. Pre-fix, `yaml.safe_load`
# in parse_model_yaml / parse_project_yaml had no try/except around it at
# all, so the underlying yaml.scanner.ScannerError / yaml.parser.ParserError
# escaped uncaught past every handler in yaml_export.py straight to
# FastAPI's default handler -- a 500, even though the bundle is malformed
# CLIENT input. These tests fail pre-fix with an *uncaught yaml.YAMLError*
# (not a clean assertion failure), which is itself the proof: the type the
# import endpoint's `except YamlImportError` handler is built to catch was
# never being raised.
# ---------------------------------------------------------------------------

def test_parse_model_yaml_malformed_syntax_raises_yaml_syntax_error_with_line_col():
    # A tab character where YAML forbids one -- a reliable scanner error
    # that PyYAML locates precisely.
    bad_yaml = "model:\n\tname: test\n"
    try:
        parse_model_yaml(bad_yaml)
        assert False, "Should have raised YamlSyntaxError"
    except YamlSyntaxError as e:
        assert e.line == 2
        assert e.column == 1
        assert "line 2" in e.errors[0]
        assert "column 1" in e.errors[0]


def test_parse_model_yaml_malformed_syntax_is_a_yaml_import_error():
    # YamlSyntaxError must satisfy the import endpoint's existing
    # `except YamlImportError` handler (services/model-service/src/api/
    # yaml_export.py::import_project_yaml), which maps it to HTTP 422, with
    # no further change needed there.
    bad_yaml = "model: [unclosed\n"
    try:
        parse_model_yaml(bad_yaml)
        assert False, "Should have raised"
    except YamlImportError as e:
        assert isinstance(e, YamlSyntaxError)


def test_parse_project_yaml_malformed_project_content_raises_yaml_syntax_error():
    bad_project = "project:\n\tname: acme\n"
    try:
        parse_project_yaml(bad_project, {})
        assert False, "Should have raised YamlSyntaxError"
    except YamlSyntaxError as e:
        assert e.line is not None
        assert e.column is not None


def test_parse_project_yaml_malformed_model_content_raises_yaml_syntax_error():
    # A malformed file under models/ must surface the same clean error as a
    # malformed project.yaml, identified via parse_model_yaml's own check.
    bad_model = "model:\n\tname: test\n"
    try:
        parse_project_yaml(
            "project:\n  name: acme\n", {"models/bad.yaml": bad_model}
        )
        assert False, "Should have raised YamlSyntaxError"
    except YamlSyntaxError as e:
        assert e.line is not None


def test_yaml_syntax_error_is_caught_by_existing_yaml_import_error_handlers():
    err = YamlSyntaxError("bad yaml", line=3, column=5)
    assert isinstance(err, YamlImportError)
    assert err.errors == ["bad yaml"]
    assert err.line == 3
    assert err.column == 5


def test_project_yaml_output():
    result = project_to_yaml(
        {"slug": "acme", "display_name": "Acme Corp"},
        [{"display_name": "warehouse", "connection_type": "postgresql"}],
    )
    doc = yaml.safe_load(result)
    assert doc["project"]["name"] == "acme"
    assert len(doc["connections"]) == 1
    assert doc["connections"][0]["type"] == "postgresql"


def test_diff_detects_added_measure():
    # differ matches entities by stable `id`; old/new must share ids, so derive
    # `new` from a deep copy of `old` rather than two independent snapshots.
    old = _make_snapshot()
    new = copy.deepcopy(old)
    new["measures"].append({
        "id": str(uuid.uuid4()),
        "model_id": new["model"]["id"],
        "name": "order_count",
        "display_name": "Order Count",
        "default_agg": "count_distinct",
        "measure_type": "standard",
    })
    result = diff_snapshots(old, new)
    added = [m for m in result["measures"]["added"] if m.get("name") == "order_count"]
    assert len(added) == 1


def test_diff_detects_removed_dimension():
    old = _make_snapshot()
    new = copy.deepcopy(old)
    new["dimensions"] = [d for d in new["dimensions"] if d["name"] != "customer_name"]
    result = diff_snapshots(old, new)
    removed = [d for d in result["dimensions"]["removed"] if d.get("name") == "customer_name"]
    assert len(removed) == 1


def test_diff_detects_modified_measure():
    old = _make_snapshot()
    new = copy.deepcopy(old)
    new["measures"][0]["default_agg"] = "average"
    result = diff_snapshots(old, new)
    modified = result["measures"]["changed"]
    assert len(modified) == 1
    assert "default_agg" in modified[0]["changes"]
    assert modified[0]["changes"]["default_agg"]["to"] == "average"


def test_diff_no_changes():
    snap = _make_snapshot()
    result = diff_snapshots(snap, snap)
    assert all(
        not (cat["added"] or cat["removed"] or cat["changed"])
        for cat in result.values()
    )
