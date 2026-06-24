"""Unit tests for the named list MDX compiler."""
import pytest

from shared.named_list_compiler import CompilationError, compile_definition, explain_definition


class TestFixedMembers:
    def test_basic_fixed_members(self):
        defn = {
            "type": "fixedMembers",
            "dimension": "Product",
            "hierarchy": "Category",
            "members": ["Bikes", "Accessories", "Clothing"],
        }
        result = compile_definition(defn)
        assert result == "{ [Product].[Category].&[Bikes], [Product].[Category].&[Accessories], [Product].[Category].&[Clothing] }"

    def test_fixed_members_dict_format(self):
        defn = {
            "type": "fixed",
            "dimension": "Geography",
            "hierarchy": "Geography",
            "members": [{"key": "US"}, {"key": "UK"}],
        }
        result = compile_definition(defn)
        assert "[Geography].[Geography].&[US]" in result
        assert "[Geography].[Geography].&[UK]" in result

    def test_fixed_members_hierarchy_defaults_to_dimension(self):
        defn = {
            "type": "fixedMembers",
            "dimension": "Color",
            "members": ["Red", "Blue"],
        }
        result = compile_definition(defn)
        assert "[Color].[Color].&[Red]" in result

    def test_fixed_members_empty_raises(self):
        defn = {"type": "fixedMembers", "dimension": "X", "members": []}
        with pytest.raises(CompilationError, match="at least one member"):
            compile_definition(defn)

    def test_fixed_members_no_dimension_raises(self):
        defn = {"type": "fixedMembers", "members": ["A"]}
        with pytest.raises(CompilationError, match="dimension"):
            compile_definition(defn)


class TestTopN:
    def test_basic_top(self):
        defn = {
            "type": "topN",
            "entity": "Customer.Customer",
            "count": 10,
            "measure": "Sales Amount",
            "direction": "top",
        }
        result = compile_definition(defn)
        assert result == "TopCount([Customer.Customer].Members, 10, [Measures].[Sales Amount])"

    def test_bottom(self):
        defn = {
            "type": "dynamic_top_n",
            "entity": "Product.Category",
            "count": 5,
            "measure": "Unit Cost",
            "direction": "bottom",
        }
        result = compile_definition(defn)
        assert result.startswith("BottomCount(")
        assert "5" in result

    def test_missing_entity_raises(self):
        defn = {"type": "topN", "count": 5, "measure": "Revenue"}
        with pytest.raises(CompilationError, match="entity"):
            compile_definition(defn)

    def test_zero_count_raises(self):
        defn = {"type": "topN", "entity": "X", "count": 0, "measure": "M"}
        with pytest.raises(CompilationError, match="count"):
            compile_definition(defn)

    def test_negative_count_raises(self):
        defn = {"type": "topN", "entity": "X", "count": -3, "measure": "M"}
        with pytest.raises(CompilationError, match="count"):
            compile_definition(defn)

    def test_missing_measure_raises(self):
        defn = {"type": "topN", "entity": "X", "count": 5}
        with pytest.raises(CompilationError, match="measure"):
            compile_definition(defn)

    def test_invalid_direction_raises(self):
        defn = {
            "type": "topN",
            "entity": "Customer",
            "count": 5,
            "measure": "Revenue",
            "direction": "middle",
        }
        with pytest.raises(CompilationError, match="direction must be"):
            compile_definition(defn)

    def test_default_direction_is_top(self):
        """Omitting direction defaults to 'top' (TopCount)."""
        defn = {
            "type": "topN",
            "entity": "Customer",
            "count": 3,
            "measure": "Revenue",
        }
        result = compile_definition(defn)
        assert result.startswith("TopCount(")

    def test_direction_case_insensitive(self):
        """Direction values 'Top'/'TOP'/'Bottom'/'BOTTOM' are accepted."""
        for direction_val, expected_prefix in [
            ("Top", "TopCount("),
            ("TOP", "TopCount("),
            ("Bottom", "BottomCount("),
            ("BOTTOM", "BottomCount("),
        ]:
            defn = {
                "type": "topN",
                "entity": "Customer",
                "count": 3,
                "measure": "Revenue",
                "direction": direction_val,
            }
            result = compile_definition(defn)
            assert result.startswith(expected_prefix), (
                f"direction={direction_val!r} should produce {expected_prefix}"
            )


class TestFilter:
    def test_single_condition(self):
        defn = {
            "type": "filter",
            "entity": "Customer.Region",
            "conditions": [
                {"field": "Sales Amount", "operator": ">", "value": 1000},
            ],
        }
        result = compile_definition(defn)
        assert result == "Filter([Customer.Region].Members, [Measures].[Sales Amount] > 1000)"

    def test_multiple_conditions_and(self):
        defn = {
            "type": "filtered",
            "entity": "Product.Product",
            "conditions": [
                {"field": "Revenue", "operator": ">=", "value": 100},
                {"field": "Cost", "operator": "<", "value": 50},
            ],
            "logic": "AND",
        }
        result = compile_definition(defn)
        assert "AND" in result
        assert "[Measures].[Revenue] >= 100" in result
        assert "[Measures].[Cost] < 50" in result

    def test_multiple_conditions_or(self):
        defn = {
            "type": "filter",
            "entity": "Region",
            "conditions": [
                {"field": "Sales", "operator": "=", "value": "High"},
                {"field": "Sales", "operator": "=", "value": "Medium"},
            ],
            "logic": "OR",
        }
        result = compile_definition(defn)
        assert " OR " in result

    def test_no_entity_raises(self):
        defn = {
            "type": "filter",
            "conditions": [{"field": "X", "operator": "=", "value": 1}],
        }
        with pytest.raises(CompilationError, match="entity"):
            compile_definition(defn)

    def test_empty_conditions_raises(self):
        defn = {"type": "filter", "entity": "X", "conditions": []}
        with pytest.raises(CompilationError, match="at least one condition"):
            compile_definition(defn)

    def test_unsupported_operator_raises(self):
        defn = {
            "type": "filter",
            "entity": "X",
            "conditions": [{"field": "Y", "operator": "LIKE", "value": "%a%"}],
        }
        with pytest.raises(CompilationError, match="Unsupported operator"):
            compile_definition(defn)

    def test_missing_field_raises(self):
        defn = {
            "type": "filter",
            "entity": "X",
            "conditions": [{"operator": "=", "value": 1}],
        }
        with pytest.raises(CompilationError, match="field"):
            compile_definition(defn)

    def test_missing_value_raises(self):
        defn = {
            "type": "filter",
            "entity": "X",
            "conditions": [{"field": "Y", "operator": "="}],
        }
        with pytest.raises(CompilationError, match="value"):
            compile_definition(defn)

    def test_string_value_quoted(self):
        defn = {
            "type": "filter",
            "entity": "X",
            "conditions": [{"field": "Name", "operator": "=", "value": "hello"}],
        }
        result = compile_definition(defn)
        assert '"hello"' in result

    def test_invalid_logic_raises(self):
        defn = {
            "type": "filter",
            "entity": "X",
            "conditions": [
                {"field": "A", "operator": "=", "value": 1},
                {"field": "B", "operator": "=", "value": 2},
            ],
            "logic": "XOR",
        }
        with pytest.raises(CompilationError, match="logic must be"):
            compile_definition(defn)

    def test_default_logic_is_and(self):
        """Omitting logic defaults to AND."""
        defn = {
            "type": "filter",
            "entity": "X",
            "conditions": [
                {"field": "A", "operator": "=", "value": 1},
                {"field": "B", "operator": "=", "value": 2},
            ],
        }
        result = compile_definition(defn)
        assert " AND " in result

    def test_logic_case_insensitive(self):
        """Logic values 'and' and 'or' (lowercase) are accepted."""
        for logic_val, expected_joiner in [("and", " AND "), ("or", " OR ")]:
            defn = {
                "type": "filter",
                "entity": "X",
                "conditions": [
                    {"field": "A", "operator": "=", "value": 1},
                    {"field": "B", "operator": "=", "value": 2},
                ],
                "logic": logic_val,
            }
            result = compile_definition(defn)
            assert expected_joiner in result


class TestUnsupportedType:
    def test_unknown_type_raises(self):
        defn = {"type": "custom_magic"}
        with pytest.raises(CompilationError, match="Unsupported builder type"):
            compile_definition(defn)

    def test_non_dict_raises(self):
        with pytest.raises(CompilationError, match="must be a dict"):
            compile_definition("not a dict")  # type: ignore


class TestExplainDefinition:
    def test_fixed_members(self):
        defn = {"type": "fixedMembers", "dimension": "Product", "members": ["A", "B"]}
        result = explain_definition(defn)
        assert "2 member(s)" in result
        assert "Product" in result

    def test_top_n(self):
        defn = {"type": "topN", "entity": "Customer", "count": 10, "measure": "Revenue", "direction": "top"}
        result = explain_definition(defn)
        assert "highest" in result
        assert "10" in result

    def test_bottom_n(self):
        defn = {"type": "dynamic_top_n", "entity": "Store", "count": 5, "measure": "Cost", "direction": "bottom"}
        result = explain_definition(defn)
        assert "lowest" in result

    def test_filter(self):
        defn = {"type": "filter", "entity": "Region", "conditions": [{"f": 1}], "logic": "OR"}
        result = explain_definition(defn)
        assert "1 condition(s)" in result
        assert "OR" in result

    def test_unknown_type(self):
        result = explain_definition({"type": "exotic"})
        assert "Advanced MDX" in result

    def test_non_dict(self):
        result = explain_definition("bad")  # type: ignore
        assert "Invalid" in result


class TestCompileWithMetadata:
    def test_metadata_passthrough(self):
        defn = {
            "type": "topN",
            "entity": "Customer",
            "count": 3,
            "measure": "Revenue",
            "direction": "top",
        }
        meta = {"dimensions": ["Customer"], "measures": ["Revenue"]}
        result = compile_definition(defn, meta)
        assert "TopCount" in result


class TestMdxEscaping:
    """F-018-17: member keys and string values must be escaped so they cannot
    break out of the generated MDX."""

    def test_member_key_closing_bracket_is_doubled(self):
        defn = {
            "type": "fixedMembers",
            "dimension": "Product",
            "hierarchy": "Product",
            "members": [{"key": "Bikes]injected"}],
        }
        out = compile_definition(defn)
        # `]` inside the key is doubled, never left bare to close the bracket.
        assert "&[Bikes]]injected]" in out

    def test_identifier_closing_bracket_is_doubled(self):
        defn = {
            "type": "topN",
            "entity": "Cust]omer",
            "count": 3,
            "measure": "Rev]enue",
        }
        out = compile_definition(defn)
        assert "[Cust]]omer]" in out
        assert "[Measures].[Rev]]enue]" in out

    def test_filter_string_value_quote_is_doubled(self):
        defn = {
            "type": "filter",
            "entity": "Customer",
            "conditions": [{"field": "Segment", "operator": "=", "value": 'A" OR 1=1'}],
        }
        out = compile_definition(defn)
        # The embedded double-quote is doubled so the literal cannot be closed.
        assert '"A"" OR 1=1"' in out

    def test_control_character_rejected(self):
        defn = {
            "type": "fixedMembers",
            "dimension": "Product",
            "members": [{"key": "bad\x00key"}],
        }
        with pytest.raises(CompilationError):
            compile_definition(defn)
