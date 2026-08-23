"""Gateway remediation-wave regression guards (2026-07-14).

Covers the root-cause fixes for:
  - Bug-6647  JDBC ``_map_type_oid`` missing ``double precision`` / time OIDs.
  - Bug-6650  XMLA member/metadata caches byte-bounded, not just entry-bounded.
  - Bug-6746  MDX bracket-body regexes handle ``]]`` escaping (dimension/hierarchy).
  - Bug-6634  ``xmla_server._sql_literal`` STRING branches route through quote_literal.
  - Bug-6951  ``_pick_content_encoding`` honours Accept-Encoding q-values (gzip;q=0).

Behaviour/contract tests — they assert wire-visible type OIDs, security-relevant
memory bounds, MDX name-extraction correctness, and HTTP content negotiation, not
implementation details.
"""
from __future__ import annotations

import struct

import pytest

from src.jdbc import protocol as proto
from src.jdbc.server import _map_type_oid
from src.jdbc import catalogue as cat
from src.dax import member_cache
from src.dax import mdx_calc_members
from src.dax import mdx_execute
from src.dax import xmla_server


# ---------------------------------------------------------------------------
# Bug-6647 — JDBC type OID mapping
# ---------------------------------------------------------------------------

class TestBug6647TypeOids:
    def test_double_precision_maps_to_float8_not_text(self):
        # Was OID_TEXT (only "double" was mapped) while _NUMERIC_CATALOGUE_TYPES
        # treats "double precision" as numeric — an inconsistency.
        assert _map_type_oid("double precision") == proto.OID_FLOAT8
        assert _map_type_oid("DOUBLE PRECISION") == proto.OID_FLOAT8

    def test_time_types_have_dedicated_oids(self):
        assert _map_type_oid("time") == proto.OID_TIME
        assert _map_type_oid("time without time zone") == proto.OID_TIME
        assert _map_type_oid("timetz") == proto.OID_TIMETZ
        assert _map_type_oid("time with time zone") == proto.OID_TIMETZ

    def test_time_oids_are_binary_encodable_on_wire(self):
        # Bug-9433 lane: the gateway now implements PG binary encoders for the
        # temporal types, so a requested binary format is HONOURED. Previously
        # these OIDs sat on a deny-list and were downgraded to text — which a
        # prepared-statement client (which fixes its formats from the STATEMENT
        # description and never re-reads the RowDescription) then decoded as
        # binary, corrupting the value.
        assert proto.OID_TIME in proto._BINARY_ENCODABLE_OIDS
        assert proto.OID_TIMETZ in proto._BINARY_ENCODABLE_OIDS
        assert proto._effective_result_format(1, proto.OID_TIME) == 1
        assert proto._effective_result_format(1, proto.OID_TIMETZ) == 1
        # And the payload really is the PG binary form, not the text bytes.
        assert proto._encode_binary_value("13:45:06.123456", proto.OID_TIME) == (
            struct.pack("!q", ((13 * 3600 + 45 * 60 + 6) * 1_000_000) + 123456)
        )
        assert proto._encode_binary_value("13:45:06+02:00", proto.OID_TIMETZ) == (
            struct.pack("!qi", (13 * 3600 + 45 * 60 + 6) * 1_000_000, -7200)
        )

    def test_catalogue_information_schema_type_names_align(self):
        # information_schema.columns pg_type name must resolve for the new types.
        assert cat._TYPE_OID_TO_NAME[cat._type_oid("double precision")] == "float8"
        assert cat._TYPE_OID_TO_NAME[cat._type_oid("time")] == "time"
        assert cat._TYPE_OID_TO_NAME[cat._type_oid("timetz")] == "timetz"

    # NUMERIC/TEMPORAL spellings — the class Bug-6647 protects. Parameterized
    # and verbose forms MUST resolve to the same wire OID on both sides (they
    # previously fell to OID_TEXT on the server side while the catalogue mapped
    # them correctly). varchar/char are deliberately EXCLUDED: the server
    # text-encodes them (OID_TEXT 25) while the catalogue advertises VARCHAR
    # (1043) in information_schema — a long-standing intentional choice, both
    # are string types, unaffected by this fix.
    @pytest.mark.parametrize("spelling", [
        "numeric(10,2)", "time(6)", "TIME(6)", "timestamp(3) with time zone",
        "double precision", "time without time zone",
        "timetz", "timestamp(3)", "  Numeric ( 10 , 2 ) ",
        "timestamp without time zone", "date", "time with time zone",
        # adversarial R3: these also diverged (catalogue lacked the keys).
        "smallint", "int2", "real", "datetime", "float4", "float8",
        "bigint", "integer",
    ])
    def test_server_and_catalogue_numeric_temporal_oids_agree(self, spelling):
        # Bug-6647 (adversarial finding): the wire RowDescription OID
        # (server._map_type_oid) must never diverge from information_schema
        # (catalogue._type_oid) for parameterized/verbose numeric/temporal
        # spellings — both modules use the same integer OID values.
        cat_oid = cat._type_oid(spelling)
        srv_oid = _map_type_oid(spelling)
        assert srv_oid == cat_oid, (
            f"{spelling!r}: server OID {srv_oid} != catalogue OID {cat_oid}"
        )
        # And the resolved OID must NOT be the TEXT fallback for these types.
        assert srv_oid != proto.OID_TEXT, (
            f"{spelling!r} fell through to OID_TEXT (the Bug-6647 defect)"
        )

    def test_precision_qualified_tz_timestamp_keeps_tz(self):
        # Secondary defect: catalogue split on '(' before checking tz, dropping
        # 'with time zone' on precision-qualified timestamps.
        assert cat._base_data_type("timestamp(3) with time zone") == (
            "timestamp with time zone"
        )
        assert cat._type_oid("timestamp(3) with time zone") == cat._OID_TIMESTAMPTZ
        assert cat._type_oid("time(6) with time zone") == cat._OID_TIMETZ


# ---------------------------------------------------------------------------
# Bug-6650 — byte-bounded caches
# ---------------------------------------------------------------------------

class TestBug6650ByteBoundedCaches:
    def test_member_cache_bounded_by_bytes(self, monkeypatch):
        member_cache._reset_for_tests()
        # Small byte cap; each value ~ a few MB of member dicts.
        monkeypatch.setattr(member_cache, "_MEMBER_MAX_BYTES", 20_000_000)
        # Keep the entry cap high so the BYTE bound is what triggers eviction.
        monkeypatch.setattr(member_cache, "_MEMBER_MAX_ENTRIES", 10_000)
        big = [{"name": "x" * 200, "key": "y" * 200} for _ in range(5000)]
        for i in range(12):
            member_cache.put_member_data(f"k{i}", list(big))
        total = sum(e[2] for e in member_cache._member_cache.values())
        assert total <= member_cache._MEMBER_MAX_BYTES, (
            f"byte bound violated: {total} > {member_cache._MEMBER_MAX_BYTES}"
        )
        # Newest survives (oldest evicted first).
        assert member_cache.get_member_data("k11") is not None

    def test_metadata_cache_bounded_by_bytes(self, monkeypatch):
        member_cache._reset_for_tests()
        monkeypatch.setattr(member_cache, "_METADATA_MAX_BYTES", 10_000_000)
        monkeypatch.setattr(member_cache, "_METADATA_MAX_ENTRIES", 10_000)
        big = ([{"name": "m" * 300}] * 3000, [], [])
        for i in range(10):
            member_cache.put_metadata(f"mk{i}", big)
        total = sum(e[2] for e in member_cache._metadata_cache.values())
        assert total <= member_cache._METADATA_MAX_BYTES

    def test_single_giant_value_still_servable(self, monkeypatch):
        member_cache._reset_for_tests()
        # A cap smaller than one value must still keep that single value.
        monkeypatch.setattr(member_cache, "_MEMBER_MAX_BYTES", 1)
        monkeypatch.setattr(member_cache, "_MEMBER_MAX_ENTRIES", 10_000)
        member_cache.put_member_data("only", [{"name": "z" * 500} for _ in range(2000)])
        assert member_cache.get_member_data("only") is not None
        assert len(member_cache._member_cache) == 1

    def test_entry_bound_still_enforced(self, monkeypatch):
        member_cache._reset_for_tests()
        monkeypatch.setattr(member_cache, "_MEMBER_MAX_ENTRIES", 3)
        for i in range(50):
            member_cache.put_member_data(f"e{i}", {"members": [i]})
        assert len(member_cache._member_cache) <= 3
        assert member_cache.get_member_data("e49") == {"members": [49]}


# ---------------------------------------------------------------------------
# Bug-6746 — MDX bracket-body ]] escaping for dimension/hierarchy names
# ---------------------------------------------------------------------------

class TestBug6746BracketEscaping:
    def test_extract_dim_member_name_handles_escaped_bracket(self):
        # Dimension "a]b" and member "m]n" -> MDX escaped [a]]b].[m]]n]
        dim, member = mdx_calc_members._extract_dim_member_name("[a]]b].[m]]n]")
        assert dim == "a]b"
        assert member == "m]n"

    def test_extract_dim_member_name_plain_names_unchanged(self):
        dim, member = mdx_calc_members._extract_dim_member_name("[country].[US]")
        assert dim == "country"
        assert member == "US"

    def test_where_dimension_member_escaped(self):
        # WHERE ([d]]im].[d]]im].[mem]]ber]) — dim "d]im", member "mem]ber".
        filters = mdx_execute._parse_where_dimension_members(
            "SELECT ... WHERE ([d]]im].[d]]im].[mem]]ber])"
        )
        assert "d]im" in filters
        assert filters["d]im"] == "mem]ber"

    def test_hierarchy_data_levels_matches_raw_name_with_bracket(self):
        # A flat dimension literally named "wei]rd" must resolve its single
        # data level even though the MDX unique name escapes the ].
        dims = [{"name": "wei]rd", "levels": ["wei]rd"]}]
        levels = mdx_execute._hierarchy_data_levels(
            "[wei]]rd].[wei]]rd]", dims, [],
        )
        assert levels == ["wei]rd"]

    def test_bb_fragment_captures_whole_escaped_body(self):
        import re
        m = re.match(rf'\[{mdx_execute._BB}\]', "[a]]b]")
        assert m is not None
        assert mdx_execute._unbracket(m.group(1)) == "a]b"

    def test_emit_side_escapes_dimension_uname_roundtrip(self):
        # Bug-6806 (absorbed): the DIMENSION_UNIQUE_NAME emitter escapes ] so the
        # parse-side reads the dimension name back whole. A flat TIME dimension
        # keeps its OWN node (not the [Dimensions] group), taking the name branch.
        from src.dax import cube_model
        uname = cube_model.dimension_unique_name_for(
            {"name": "we]rd", "is_time_dim": True}
        )
        assert uname == "[we]]rd]"
        # Round-trip: the mdx_execute dim extractor recovers "we]rd".
        import re
        m = re.match(rf'\[{mdx_execute._BB}\]', uname)
        assert m and mdx_execute._unbracket(m.group(1)) == "we]rd"

    def test_qualify_member_uname_escapes_level_name(self):
        from src.dax.member_uname import qualify_member_uname
        out = qualify_member_uname("[d].[h]", "lev]el", ["k]1"])
        # level and key both escaped
        assert "[lev]]el]" in out
        assert "&[k]]1]" in out

    def test_calc_member_circular_ref_unescapes_measure_name(self):
        # Bug-6746 (Stage-1 finding): sibling parsers in mdx_calc_members must
        # also be escape-aware. A calc member referencing a ]-named measure must
        # resolve the dependency (not truncate at the escaped bracket).
        from src.dax.mdx_calc_members import _check_circular_references, CalcMember
        a = CalcMember(name="a]b", expression="[Measures].[a]]b] + 1")
        # Self-reference must be detected as a cycle (name round-trips whole).
        import pytest as _pytest
        with _pytest.raises(ValueError):
            _check_circular_references([a])

    def test_calc_member_find_base_measure_unescapes(self):
        from src.dax.mdx_calc_members import _find_base_measure
        assert _find_base_measure("[Measures].[re]]v] * 2") == "re]v"

    def test_calc_member_aggregate_set_unescapes(self):
        from src.dax.mdx_calc_members import _classify_dim_expression, CalcMember
        calc = CalcMember(
            name="grp", expression="Aggregate({[geo].[geo].[ci]]ty]})",
        )
        _classify_dim_expression(calc)
        assert calc.calc_type == "aggregate_set"
        assert "ci]ty" in calc.aggregate_members

    def test_calc_atom_compile_unescapes_row_key(self):
        from src.dax.mdx_calc_members import _compile_atom
        fn = _compile_atom("[Measures].[re]]v]")
        # Keys into the raw result row under the UNESCAPED name.
        assert fn({"re]v": 5}) == 5.0


# ---------------------------------------------------------------------------
# Bug-6634 — string literals route through quote_literal
# ---------------------------------------------------------------------------

class TestBug6634LiteralEscaping:
    def test_string_literal_quotes_single_quote(self):
        assert xmla_server._sql_literal("O'Reilly", is_string=True) == "'O''Reilly'"

    def test_numeric_string_kept_quoted_when_typed_string(self):
        # A zero-padded code declared string must stay quoted, not become numeric.
        assert xmla_server._sql_literal("00123", is_string=True) == "'00123'"

    def test_clean_numeric_emitted_verbatim(self):
        assert xmla_server._sql_literal("42", is_string=False) == "42"

    def test_backslash_value_contained(self):
        # sqlglot postgres literal preserves backslash (standard-conforming),
        # doubles the quote — value stays contained.
        out = xmla_server._sql_literal("a\\b", is_string=True)
        assert out.startswith("'") and out.endswith("'")
        assert "a\\b" in out


# ---------------------------------------------------------------------------
# Bug-6951 — Accept-Encoding q-value negotiation
# ---------------------------------------------------------------------------

class TestBug6951ContentEncoding:
    @pytest.mark.parametrize("header,expected", [
        ("gzip, deflate", "gzip"),
        ("gzip;q=0", ""),                       # explicit refusal
        ("gzip;q=0, deflate", "deflate"),
        ("gzip;q=0.5, deflate;q=0.8", "deflate"),
        ("gzip;q=1.0, deflate;q=1.0", "gzip"),  # tie -> gzip
        ("identity", ""),
        ("", ""),
        ("*", "gzip"),
        ("gzip;q=0, deflate;q=0", ""),
        ("deflate;q=0", ""),
        ("gzip;q=0, *;q=0", ""),
    ])
    def test_pick_content_encoding_respects_qvalues(self, header, expected):
        assert xmla_server._pick_content_encoding(header) == expected
