"""Bug-9487 XMLA consumer guard for numeric year identity and caption."""
from src.dax.mdx_execute import build_real_execute_response


def test_bug9487_r1_u1_xmla_member_uses_numeric_uname_and_span_caption():
    xml = build_real_execute_response(
        mdx="SELECT {[Measures].[Amount]} ON COLUMNS, {[Fiscal Year].[Fiscal Year].Members} ON ROWS FROM [demo]",
        catalog="demo",
        columns=["Fiscal Year", "Fiscal Year__caption", "Amount"],
        rows=[{"Fiscal Year": 2025, "Fiscal Year__caption": "2025-26", "Amount": 10}],
        measures_meta=[{"name": "Amount", "default_agg": "sum"}],
        dimensions_meta=[{"name": "Fiscal Year", "display_column_name": "year_label"}],
    )
    assert "2025-26" in xml
    assert "2025" in xml
    assert "2025-26" not in xml.split("<UName>", 1)[-1].split("</UName>", 1)[0]
