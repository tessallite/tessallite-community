"""Heuristic auto-analysis for a ModelTable and its columns.

Given a table record (with eager-loaded columns), produces suggestions for:
- table_type  (fact vs dim_aggregate vs dim_detail)
- dimension columns worth flagging
- potential measure columns
- date/time columns that could anchor a calendar
- measure-vs-dimension validation warnings (dual-signal: name + cardinality)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from shared.semantic.graph_order import FACT_TABLE_TYPE
from shared.type_family import (
    DATETIME as _FAM_DATETIME,
    NUMERIC as _FAM_NUMERIC,
    TEXT as _FAM_TEXT,
    type_family,
)

if TYPE_CHECKING:
    from shared.db.models import ModelTable

_DATE_PATTERNS = re.compile(
    r"date|_dt|_at|_time|timestamp|created|updated|ordered|shipped|"
    r"posted|occurred|start|end|period|year|month|week|day",
    re.IGNORECASE,
)
_MEASURE_PATTERNS = re.compile(
    r"amount|qty|quantity|count|total|sum|revenue|cost|price|value|"
    r"sales|units|margin|profit|loss|balance|volume|weight|size|rate",
    re.IGNORECASE,
)
_DIM_PATTERNS = re.compile(
    r"_id$|_key$|_code$|_name$|category|type|status|flag|region|"
    r"country|city|segment|channel|tier|class|group|level",
    re.IGNORECASE,
)

# F-014-02: classify on canonical type *family* (``shared.type_family``) instead
# of a PostgreSQL-only literal-spelling set, so BigQuery (``int64``/``float64``),
# Snowflake (``number``), SQL Server (``decimal``), and Spark (``double``)
# numeric columns are recognised as measure candidates rather than ignored.

# Name patterns that strongly indicate a numeric column is a dimension, not a
# measure — even though the data type is numeric.
_NUMERIC_DIM_PATTERNS = re.compile(
    r"_id$|_key$|_pk$|_sk$|_fk$|_code$|_no$|_num$|_number$|"
    r"^id$|^pk$|^sk$|"
    r"zip|postal|phone|fax|ssn|ein|isbn",
    re.IGNORECASE,
)

# Ambiguous patterns: could be measure or dimension depending on cardinality.
# Examples: score (test score vs. score band), class (class label vs. class
# number), group (group ID vs. group count).
_AMBIGUOUS_PATTERNS = re.compile(
    r"_score$|_class$|_group$|_grp$|_cls$|_rank$|_tier$|_band$|_bucket$",
    re.IGNORECASE,
)

# A table is likely a fact if the majority of its columns look like measures
# or FK references rather than descriptive text columns.
_FACT_MEASURE_THRESHOLD = 0.25  # >= 25 % of columns match measure patterns

# Cardinality thresholds for dual-signal validation
_LOW_CARDINALITY_RATIO = 0.01    # < 1% distinct values -> likely dimension
_LOW_CARDINALITY_MAX = 100       # absolute cap for "low cardinality" classification


@dataclass
class ColumnSuggestion:
    column_id: str
    column_name: str
    suggested_role: str  # "measure" | "dimension" | "date_key" | "ignore"
    reason: str


@dataclass
class MeasureWarning:
    """A warning that a column classified as a measure may actually be a dimension."""
    column_id: str
    column_name: str
    current_role: str
    suggested_role: str
    severity: str  # "high" | "medium" | "low"
    reason: str


@dataclass
class TableAnalysisResult:
    table_id: str
    suggested_table_type: str  # "fact" | "dim_aggregate" | "dim_detail"
    confidence: str             # "high" | "medium" | "low"
    reasoning: str
    column_suggestions: list[ColumnSuggestion] = field(default_factory=list)
    date_columns: list[str] = field(default_factory=list)
    potential_calendar_column: str | None = None
    measure_warnings: list[MeasureWarning] = field(default_factory=list)


def _classify_column(
    name: str,
    dtype: str,
    cardinality: int | None,
    row_count: int | None,
    table_type: str,
) -> tuple[str, str]:
    """Classify a single column, returning (role, reason).

    Uses a dual-signal approach: name patterns + cardinality ratio.
    Only suggests 'measure' for columns in fact tables.
    """
    fam = type_family(dtype)
    is_numeric = fam == _FAM_NUMERIC
    is_date = fam == _FAM_DATETIME

    if is_date or _DATE_PATTERNS.search(name):
        return "date_key", "data type or name indicates a date/time value"

    if _NUMERIC_DIM_PATTERNS.search(name):
        return "dimension", "numeric column with identifier/code name pattern"

    if _DIM_PATTERNS.search(name):
        return "dimension", "name matches dimension/foreign-key pattern"

    if is_numeric and _MEASURE_PATTERNS.search(name):
        if table_type != FACT_TABLE_TYPE:
            return "dimension", "numeric measure-like name but table is not a fact table"
        return "measure", "numeric column with measure-like name in fact table"

    if is_numeric and _AMBIGUOUS_PATTERNS.search(name):
        if _has_low_cardinality(cardinality, row_count):
            return "dimension", "ambiguous name with low cardinality — likely a categorical attribute"
        if table_type == FACT_TABLE_TYPE:
            return "measure", "ambiguous name with high cardinality in fact table — likely a measure"
        return "dimension", "ambiguous name in non-fact table"

    if is_numeric and table_type == FACT_TABLE_TYPE:
        if _has_low_cardinality(cardinality, row_count):
            return "dimension", "numeric column with low cardinality in fact table — likely a degenerate dimension"
        return "measure", "unrecognised numeric column in fact table — defaulting to measure"

    if is_numeric:
        return "dimension", "numeric column in non-fact table"

    if fam == _FAM_TEXT:
        return "dimension", "text column"

    return "ignore", "no clear role detected"


def _has_low_cardinality(cardinality: int | None, row_count: int | None) -> bool:
    if cardinality is None or row_count is None or row_count == 0:
        return False
    ratio = cardinality / row_count
    return ratio < _LOW_CARDINALITY_RATIO and cardinality <= _LOW_CARDINALITY_MAX


def _validate_measure(
    name: str,
    dtype: str,
    cardinality: int | None,
    row_count: int | None,
    column_id: str,
) -> MeasureWarning | None:
    """Check if a column currently tagged as a measure is likely a dimension.

    Returns a MeasureWarning if suspicious, None otherwise.
    """
    if type_family(dtype) != _FAM_NUMERIC:
        return None

    # Signal 1: Name pattern strongly suggests a dimension
    if _NUMERIC_DIM_PATTERNS.search(name):
        return MeasureWarning(
            column_id=column_id,
            column_name=name,
            current_role="measure",
            suggested_role="dimension",
            severity="high",
            reason=f"Column '{name}' has an identifier/code name pattern (_id, _key, _code, _no, _number) "
                   "and is likely a dimension, not a measure. Aggregating it (SUM, AVG) would produce "
                   "meaningless results.",
        )

    # Signal 2: Low cardinality — few distinct values relative to row count
    if cardinality is not None and row_count and row_count > 0:
        ratio = cardinality / row_count
        if ratio < _LOW_CARDINALITY_RATIO and cardinality <= _LOW_CARDINALITY_MAX:
            return MeasureWarning(
                column_id=column_id,
                column_name=name,
                current_role="measure",
                suggested_role="dimension",
                severity="medium",
                reason=f"Column '{name}' has only {cardinality} distinct values out of "
                       f"{row_count:,} rows ({ratio:.2%} cardinality). This is typical of a "
                       "categorical dimension, not a continuous measure.",
            )

    # Signal 3: Ambiguous name pattern — warn but don't block
    if _AMBIGUOUS_PATTERNS.search(name):
        if cardinality is not None and row_count and row_count > 0:
            ratio = cardinality / row_count
            if ratio < 0.05 and cardinality <= 500:
                return MeasureWarning(
                    column_id=column_id,
                    column_name=name,
                    current_role="measure",
                    suggested_role="dimension",
                    severity="low",
                    reason=f"Column '{name}' has an ambiguous name (_score, _class, _group) and "
                           f"relatively low cardinality ({cardinality} distinct values). Consider "
                           "whether this is used for filtering/grouping (dimension) or aggregation (measure).",
                )

    return None


def analyze_table(table: "ModelTable") -> TableAnalysisResult:
    columns = list(table.columns or [])

    row_count = getattr(table, "row_count_estimate", None)
    table_type = table.table_type or ""

    date_cols: list[str] = []
    measure_cols: list[str] = []
    dim_cols: list[str] = []
    col_suggestions: list[ColumnSuggestion] = []

    for col in columns:
        name = col.column_name
        dtype = (col.data_type or "").lower()
        cardinality = getattr(col, "cardinality_estimate", None)

        role, reason = _classify_column(name, dtype, cardinality, row_count, table_type)

        if role == "date_key":
            date_cols.append(name)
        elif role == "measure":
            measure_cols.append(name)
        elif role == "dimension":
            dim_cols.append(name)

        col_suggestions.append(ColumnSuggestion(
            column_id=str(col.id),
            column_name=name,
            suggested_role=role,
            reason=reason,
        ))

    # Decide table type
    n = len(columns) or 1
    measure_ratio = len(measure_cols) / n

    current_type = table_type

    if current_type == FACT_TABLE_TYPE:
        suggested_type = "fact"
        confidence = "high"
        reasoning = "Table is already classified as fact."
    elif measure_ratio >= _FACT_MEASURE_THRESHOLD and len(date_cols) >= 1:
        suggested_type = "fact"
        confidence = "high" if measure_ratio >= 0.4 else "medium"
        reasoning = (
            f"{len(measure_cols)} measure column(s) ({measure_ratio:.0%} of total) "
            f"and {len(date_cols)} date column(s) suggest a fact table."
        )
    elif len(dim_cols) > len(measure_cols) and len(measure_cols) == 0:
        suggested_type = "dim_detail"
        confidence = "medium"
        reasoning = (
            f"{len(dim_cols)} dimension-like column(s) and no numeric measures "
            "suggest a detail dimension."
        )
    elif len(columns) <= 10 and len(dim_cols) > 0:
        suggested_type = "dim_aggregate"
        confidence = "low"
        reasoning = "Small table with dimension-like columns; could be an aggregate dimension."
    else:
        suggested_type = current_type or "dim_detail"
        confidence = "low"
        reasoning = "Insufficient signal to determine table type; defaulting to dim_detail."

    # Pick the most likely calendar anchor column
    calendar_col: str | None = None
    for name in date_cols:
        if re.search(r"order|transact|event|created", name, re.IGNORECASE):
            calendar_col = name
            break
    if calendar_col is None and date_cols:
        calendar_col = date_cols[0]

    return TableAnalysisResult(
        table_id=str(table.id),
        suggested_table_type=suggested_type,
        confidence=confidence,
        reasoning=reasoning,
        column_suggestions=col_suggestions,
        date_columns=date_cols,
        potential_calendar_column=calendar_col,
    )


def validate_measures(
    table: "ModelTable",
    measure_column_names: list[str],
) -> list[MeasureWarning]:
    """Validate existing measure classifications for a table.

    Takes a list of column names that are currently tagged as measures and
    returns warnings for any that look like they should be dimensions.

    Uses dual-signal heuristics: name patterns + cardinality ratio.
    """
    row_count = getattr(table, "row_count_estimate", None)
    warnings: list[MeasureWarning] = []
    measure_set = {n.lower() for n in measure_column_names}

    for col in (table.columns or []):
        if col.column_name.lower() not in measure_set:
            continue
        dtype = (col.data_type or "").lower()
        cardinality = getattr(col, "cardinality_estimate", None)
        w = _validate_measure(
            col.column_name, dtype, cardinality, row_count, str(col.id),
        )
        if w is not None:
            warnings.append(w)

    return warnings
