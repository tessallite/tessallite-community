"""Lane L1 piggybacks — Bug-6773, Bug-6807 and Bug-9116.

* **Bug-6773** — the second-order relation-collision fallback appended its
  counter AFTER the ``$KPIs`` marker (``alpha__sales$KPIs_2``). Consumers
  recognise a scorecard relation by that suffix, so the advertised relation
  became permanently unqueryable: the scorecard interception missed, the binder
  could not resolve it, and every query against it 422'd.
* **Bug-6807** — value normalisation keyed off CATALOGUE column names, so a
  custom alias matching no catalogue column skipped Bug-5383's aggregate
  formatting entirely. ``SUM(revenue) AS total_rev`` handed the client
  ``1.0E+5`` as text while the identical ``SUM(revenue) AS revenue`` rendered
  ``100000`` — the formatting depended on what the user called the column.
* **Bug-9116** — the Describe/typing side channel reads connection-time column
  metadata and never revalidated it. Bug-9433's ``SELECT *`` expansion turned
  that from "confirms a column the client already named" into "ENUMERATES the
  field list", so the enumerating case now revalidates CLS first — and ordinary
  query dispatch still must not (that is Bug-9112 / SOL-LAT-001's contract).

Execution scope: isolated. Gate tier: T2 (fixed-bug regression guards).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src import router_client
from src.jdbc import protocol as proto
from src.jdbc.server import PGWireServer


# ---------------------------------------------------------------------------
# Bug-6773 — the $KPIs suffix is structural
# ---------------------------------------------------------------------------


def test_collision_counter_goes_before_the_kpis_marker():
    """Pre-fix this produced ``project__sales$KPIs_2``, which no longer ends
    with ``$KPIs`` and so is no longer a scorecard relation to any consumer."""
    assert router_client._suffix_preserving_counter("project__sales$KPIs", 2) == (
        "project__sales_2$KPIs"
    )
    assert router_client._suffix_preserving_counter("project__sales$KPIs", 3) == (
        "project__sales_3$KPIs"
    )


def test_a_plain_relation_keeps_the_ordinary_trailing_counter():
    assert router_client._suffix_preserving_counter("project__sales", 2) == (
        "project__sales_2"
    )


def test_the_marker_match_is_case_insensitive_like_its_consumers():
    """``_is_kpi_table_query`` lower-cases before testing the suffix, so the
    counter placement must recognise the same spellings its consumers do."""
    assert router_client._suffix_preserving_counter("p__sales$kpis", 2) == (
        "p__sales_2$kpis"
    )


def test_every_generated_name_is_still_recognised_as_a_scorecard():
    """The property that actually matters, checked against the real consumer."""
    server = PGWireServer()
    for counter in (2, 3, 4):
        name = router_client._suffix_preserving_counter("alpha__sales$KPIs", counter)
        server._table_model_id = {name: "m-1"}
        assert server._is_kpi_table_query(f'SELECT * FROM "{name}"'), (
            f"{name!r} is no longer recognised as a $KPIs relation"
        )


# ---------------------------------------------------------------------------
# Bug-6807 — normalisation must not depend on the alias the user chose
# ---------------------------------------------------------------------------


def _server_with_columns():
    server = PGWireServer()
    server._model_names = ["modelx"]
    server._table_columns = {
        "modelx": [
            {"name": "revenue", "data_type": "numeric"},
            {"name": "region", "data_type": "text"},
            {"name": "created_at", "data_type": "timestamp"},
        ]
    }
    server._table_model_id = {"modelx": "m-1"}
    return server


def test_custom_aliased_aggregate_is_treated_as_numeric():
    """``SUM(revenue) AS total_rev`` matches no catalogue column, so pre-fix it
    was not "numeric" and the router's ``1.0E+5`` reached the client verbatim."""
    server = _server_with_columns()
    numeric = server._numeric_result_columns(
        "SELECT region, SUM(revenue) AS total_rev FROM modelx GROUP BY region"
    )
    assert "total_rev" in numeric
    assert "region" not in numeric


def test_the_colliding_alias_case_is_unchanged():
    """``SUM(revenue) AS revenue`` was already covered; it must stay covered."""
    server = _server_with_columns()
    assert "revenue" in server._numeric_result_columns(
        "SELECT SUM(revenue) AS revenue FROM modelx"
    )


def test_a_non_numeric_inner_column_does_not_become_numeric():
    """The fix must not widen into genuine text/temporal columns — that would
    re-run numeric reshaping over values that are not numbers."""
    server = _server_with_columns()
    numeric = server._numeric_result_columns(
        "SELECT MAX(created_at) AS latest, MIN(region) AS first_region FROM modelx"
    )
    assert "latest" not in numeric
    assert "first_region" not in numeric


def test_bug6807_text_expression_over_numeric_column_is_not_normalised():
    """Bug-6807: a text-producing expression must remain text.

    The source column is numeric, but CONCAT/CAST makes the result a label. A
    column-name-only heuristic incorrectly normalised the scientific-looking
    label as a number before this guard.
    """
    server = _server_with_columns()
    sql = "SELECT CONCAT(CAST(revenue AS TEXT), 'E+5') AS label FROM modelx"
    numeric = server._numeric_result_columns(sql)
    assert "label" not in numeric
    from src.jdbc.server import _normalize_jdbc_row

    row = _normalize_jdbc_row({"label": "1E+5"}, ["label"], numeric)
    assert row == ["1E+5"]


def test_count_star_is_numeric_even_though_it_reads_no_column():
    server = _server_with_columns()
    assert "n" in server._numeric_result_columns(
        "SELECT COUNT(*) AS n FROM modelx"
    )


def test_the_end_to_end_effect_scientific_notation_is_normalised():
    """The user-visible outcome: the value, not the flag."""
    from src.jdbc.server import _normalize_jdbc_row

    server = _server_with_columns()
    sql = "SELECT SUM(revenue) AS total_rev FROM modelx"
    numeric = server._numeric_result_columns(sql)
    row = _normalize_jdbc_row({"total_rev": "1.0E+5"}, ["total_rev"], numeric)
    assert row == ["100000"], (
        "scientific notation reached the client verbatim (Bug-6807)"
    )


@pytest.mark.parametrize(
    "expression",
    [
        "COALESCE(SUM(revenue), 0)",
        "ROUND(SUM(revenue), 0)",
        "ABS(SUM(revenue))",
    ],
    ids=["coalesce", "round", "abs"],
)
def test_bug6807_numeric_wrappers_are_classified_and_normalised(expression):
    """Bug-6807/Bug-9508: supported numeric wrappers keep aggregate output numeric.

    Before the wrapper whitelist, the alias was absent from the numeric
    result set and the JDBC row retained the source's scientific notation.
    The text-producing CONCAT/CAST regression above must remain nonnumeric.
    """
    from src.jdbc.server import _normalize_jdbc_row

    server = _server_with_columns()
    numeric = server._numeric_result_columns(
        f"SELECT {expression} AS total_rev FROM modelx"
    )
    assert "total_rev" in numeric
    assert _normalize_jdbc_row(
        {"total_rev": "1.0E+5"}, ["total_rev"], numeric
    ) == ["100000"]


# ---------------------------------------------------------------------------
# Bug-9116 / Bug-9112 — which describes revalidate CLS, and which must not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_enumerating_describe_revalidates_cls_first():
    """``SELECT *`` describes by ENUMERATING the connection-time field list, so
    after a mid-session CLS tightening it could echo a now-restricted column
    name. It revalidates first, exactly as a catalogue query does."""
    server = _server_with_columns()
    server._catalogue = object()  # only its presence is read here
    with patch.object(
        server, "_refresh_catalogue_if_stale", new=AsyncMock()
    ) as refresh:
        await server._describe_columns_for_client("SELECT * FROM modelx")
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_explicit_projection_describe_does_not_reload_metadata():
    """The documented connection-time-capture contract for explicit projections
    is unchanged, and it costs nothing — a client that names its columns cannot
    learn a name it did not already have."""
    server = _server_with_columns()
    server._catalogue = object()
    with patch.object(
        server, "_refresh_catalogue_if_stale", new=AsyncMock()
    ) as refresh:
        await server._describe_columns_for_client("SELECT region FROM modelx")
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unparseable_describe_revalidates_conservatively():
    server = _server_with_columns()
    server._catalogue = object()
    with patch.object(
        server, "_refresh_catalogue_if_stale", new=AsyncMock()
    ) as refresh:
        await server._describe_columns_for_client("SELECT * FROM (((")
    refresh.assert_awaited_once()


def test_the_describe_seam_is_not_a_third_query_dispatch_reload():
    """Bug-9112 blind-spot guard.

    SOL-LAT-001 removed the tenant-metadata reload from the two EXECUTE seams.
    Bug-9116 adds a refresh at the DESCRIBE seam, which is a third place the
    reload can creep back onto the hot path. Pin the predicate that bounds it:
    ordinary query shapes — the ones dispatch actually sends — must not qualify.
    """
    from src.jdbc.server import _projection_has_star

    for ordinary in (
        "SELECT region, SUM(revenue) FROM modelx GROUP BY region",
        "SELECT region FROM modelx WHERE revenue > 10",
        "SELECT COUNT(*) FROM modelx",
        "SET app.region = 'EMEA'",
    ):
        assert _projection_has_star(ordinary) is False, ordinary

    for enumerating in (
        "SELECT * FROM modelx",
        "SELECT m.* FROM modelx m",
        "SELECT *, region FROM modelx",
    ):
        assert _projection_has_star(enumerating) is True, enumerating


def test_type_oid_helper_is_shared_so_describe_and_execute_cannot_drift():
    """Both sides map catalogue types through the same helper; a divergence
    here is the Bug-6776 corruption path."""
    from src.jdbc.server import _map_type_oid

    server = _server_with_columns()
    shape = dict(server._describe_columns_metadata_only("SELECT * FROM modelx"))
    assert shape["revenue"] == _map_type_oid("numeric") == proto.OID_NUMERIC
    assert shape["region"] == _map_type_oid("text") == proto.OID_TEXT
    assert shape["created_at"] == _map_type_oid("timestamp") == proto.OID_TIMESTAMP
