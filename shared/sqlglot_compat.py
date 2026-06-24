"""Shared sqlglot dialect patches for gaps in sqlglot 30.x."""
from __future__ import annotations

from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery as _BigQueryDialect


def _bq_splitpart_sql(
    self: _BigQueryDialect.Generator, expression: exp.SplitPart
) -> str:
    """SPLIT_PART(s, d, n) -> SPLIT(s, d)[OFFSET(n - 1)]"""
    s = self.sql(expression, "this")
    d = self.sql(expression, "delimiter")
    idx = expression.args.get("part_index")
    if isinstance(idx, exp.Literal) and idx.is_int:
        offset = str(int(idx.this) - 1)
    else:
        offset = f"{self.sql(idx)} - 1"
    return f"SPLIT({s}, {d})[OFFSET({offset})]"


def register_bigquery_patches() -> None:
    """Register all BigQuery dialect patches. Safe to call multiple times."""
    if exp.SplitPart not in _BigQueryDialect.Generator.TRANSFORMS:
        _BigQueryDialect.Generator.TRANSFORMS[exp.SplitPart] = _bq_splitpart_sql
