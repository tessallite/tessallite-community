"""Render a chart as an HTML string using Charts.css semantic tables.

Charts.css styles ordinary <table> elements into charts via CSS classes.
KPI tiles are rendered as plain HTML — no chart framework needed.
"""
from __future__ import annotations

import html
import json
import secrets
from collections import OrderedDict
from decimal import Decimal
from typing import Any


_SIZE_MAP = {
    "sm": "max-width:400px;height:200px",
    "md": "max-width:600px;height:300px",
    "lg": "max-width:800px;height:400px",
}

_LINE_SIZE_MAP = {
    "sm": "width:100%;max-width:none;height:220px",
    "md": "width:100%;max-width:none;height:300px",
    "lg": "width:100%;max-width:none;height:400px",
}

_PALETTES: dict[str, list[str]] = {
    "tessallite": [
        "#006C35", "#D4AF37", "#3A5EA8", "#7B3FA0", "#B33A3A",
        "#A67C00", "#004E25", "#1A2D5A", "#4A1870", "#5A6577",
    ],
    "default": [
        "#006C35", "#D4AF37", "#3A5EA8", "#7B3FA0", "#B33A3A",
        "#A67C00", "#004E25", "#1A2D5A", "#4A1870", "#5A6577",
    ],
    "muted": [
        "#8FBC8F", "#F5DEB3", "#B0C4DE", "#D8BFD8", "#F4A6A6",
        "#E6D5A8", "#A8D5A2", "#A8C0D8", "#C4A8D8", "#C0C0C0",
    ],
    "high_contrast": [
        "#004E25", "#FFD700", "#0047AB", "#C71585", "#DC143C",
        "#FF8C00", "#006400", "#00008B", "#8B008B", "#2F4F4F",
    ],
    "colorblind_safe": [
        "#0072B2", "#E69F00", "#009E73", "#CC79A7", "#D55E00",
        "#56B4E9", "#F0E442", "#999999", "#882255", "#44AA99",
    ],
}


def _wrapper_id() -> str:
    return f"tsc-{secrets.token_hex(4)}"


def _palette_style(palette: str, wrapper_id: str) -> str:
    colors = _PALETTES.get(palette, _PALETTES["tessallite"])
    rules = " ".join(f"--color-{i + 1}: {c};" for i, c in enumerate(colors))
    return (
        f"#{wrapper_id} .charts-css {{ --labels-size: 3rem; {rules} }} "
        f"#{wrapper_id} .charts-css th {{ white-space: nowrap; font-size: 12px; line-height: 1.15; }}"
    )


def _num(v: Any) -> float | None:
    """Coerce a value to float. Returns None for non-numeric types."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _esc(value: Any) -> str:
    return html.escape(str(value) if value is not None else "")


def _display_value(value: Any) -> str:
    n = _num(value)
    if n is None:
        return str(value) if value is not None else ""
    return f"{n:,.2f}" if n != int(n) else f"{int(n):,}"


def _build_table(columns: list[str], rows: list[list[Any]]) -> str:
    header_style = (
        "padding:8px 10px;text-align:left;font-weight:700;"
        "border-bottom:1px solid #b8c8c0;border-right:1px solid #d7e1dc;"
        "background:#f3f7f4;color:#0f1f18;white-space:nowrap"
    )
    cell_base_style = (
        "padding:7px 10px;border-bottom:1px solid #d7e1dc;"
        "border-right:1px solid #e1e8e4;color:#0f1f18;white-space:nowrap"
    )
    cells_header = "".join(f'<th style="{header_style}">{_esc(c)}</th>' for c in columns)
    body_rows = []
    for row in rows:
        cells = "".join(
            f'<td style="{cell_base_style};text-align:{"right" if _num(v) is not None else "left"};'
            'font-variant-numeric:tabular-nums">'
            f'{_esc(_display_value(v))}</td>'
            for v in row
        )
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        '<div class="rendered-data-table" '
        'style="overflow-x:auto;margin-top:12px;border:1px solid #c8d6cf;'
        'border-radius:4px;background:#fff">'
        '<table style="border-collapse:collapse;font-size:12px;width:100%;margin:0">'
        f"<thead><tr>{cells_header}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div>"
    )


def render_table(columns: list[str], rows: list[list[Any]]) -> str:
    if not columns or not rows:
        return ""
    return _build_table(columns, rows)


def _json_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            return None
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    return str(value)


def _artifact_rows(columns: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({
            col: _json_value(row[idx] if idx < len(row) else None)
            for idx, col in enumerate(columns)
        })
    return out


def render_visual_artifact(
    chart_type: str | None,
    columns: list[str],
    rows: list[list[Any]],
    palette: str = "default",
    size: str = "md",
    include_table: bool = True,
    legacy_html: str | None = None,
) -> str:
    if not columns or not rows:
        return ""
    payload = {
        "kind": "tessallite.visual.v1",
        "renderer": "echarts",
        "chart_type": chart_type,
        "columns": list(columns),
        "rows": _artifact_rows(columns, rows),
        "palette": palette,
        "size": size,
        "include_table": include_table,
    }
    if legacy_html:
        payload["legacy_html"] = legacy_html
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _render_kpi(value: Any) -> str:
    formatted = _display_value(value)
    return (
        '<div style="text-align:center;padding:24px 0">'
        f'<span style="font-size:2.5rem;font-weight:700">{_esc(formatted)}</span>'
        "</div>"
    )


def _render_multi_kpi(columns: list[str], row: list[Any]) -> str:
    tiles = []
    for i, col in enumerate(columns):
        val = row[i] if i < len(row) else None
        formatted = _display_value(val)
        label = col.replace("_", " ").title()
        tiles.append(
            '<div style="flex:1;text-align:center;padding:16px 8px;'
            'min-width:140px">'
            f'<div style="font-size:12px;font-weight:600;color:#666;'
            f'margin-bottom:4px">{_esc(label)}</div>'
            f'<div style="font-size:1.8rem;font-weight:700">'
            f'{_esc(formatted)}</div>'
            '</div>'
        )
    return (
        '<div style="display:flex;flex-wrap:wrap;justify-content:center;'
        'gap:8px;padding:16px 0">'
        + "".join(tiles)
        + '</div>'
    )


def _max_measure(rows: list[list[Any]], col_idx: int = 1) -> float:
    vals = [abs(n) for r in rows
            if len(r) > col_idx and (n := _num(r[col_idx])) is not None]
    return max(vals, default=1) or 1


# F-023-20 — bar/column bars are sized by magnitude (Charts.css cannot draw
# below the baseline), so a negative value would otherwise be visually
# identical to a positive one of the same size. Negative cells get a
# distinct fill colour and the displayed number keeps its sign, so a loss
# month in a profit chart is unmistakable at a glance.
_NEGATIVE_BAR_COLOR = "#B33A3A"


def _negative_cell_style(val: float | None) -> str:
    """Return an inline ``--color`` override for negative measure cells."""
    if val is not None and val < 0:
        return f"; --color: {_NEGATIVE_BAR_COLOR}"
    return ""


def _chart_title(text: str) -> str:
    if not text:
        return ""
    return (
        f'<div style="font-size:13px;font-weight:600;margin-bottom:4px;'
        f'color:#444">{_esc(text)}</div>'
    )


def _legend_html(labels: list[str], palette: str) -> str:
    colors = _PALETTES.get(palette, _PALETTES["tessallite"])
    parts = []
    for i, label in enumerate(labels):
        c = colors[i % len(colors)]
        parts.append(
            f'<span style="display:inline-block;width:12px;height:12px;'
            f'background:{c};margin-right:4px"></span>{_esc(label)}&nbsp;&nbsp;'
        )
    return f'<div style="font-size:12px;margin-bottom:4px">{" ".join(parts)}</div>'


def _axis_labels_html(labels: list[Any]) -> str:
    if not labels:
        return ""
    cells = "".join(
        '<span style="overflow:hidden;text-overflow:clip;white-space:nowrap">'
        f"{_esc(label)}</span>"
        for label in labels
    )
    return (
        '<div class="ts-axis-labels" '
        f'style="display:grid;grid-template-columns:repeat({len(labels)},minmax(0,1fr));'
        'gap:4px;margin-top:8px;font-size:11px;font-weight:600;color:#111;'
        'line-height:1.1;text-align:center">'
        f"{cells}</div>"
    )


def _line_chart_hidden_label_style(wrapper_id: str) -> str:
    return f"#{wrapper_id}.ts-line-chart .charts-css th {{ color: transparent; }}"


def _svg_polyline(points: list[tuple[float, float]]) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def _nice_axis_bounds(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 1.0
    min_val = min(values)
    max_val = max(values)
    if min_val == max_val:
        if min_val == 0:
            return 0.0, 1.0
        pad = abs(min_val) * 0.1
        return min_val - pad, max_val + pad
    spread = max_val - min_val
    pad = spread * 0.08
    if min_val >= 0 and min_val - pad < 0:
        return 0.0, max_val + pad
    return min_val - pad, max_val + pad


def _render_multi_metric_line_svg(
    columns: list[str],
    rows: list[list[Any]],
    measure_indices: list[int],
    palette: str,
    size: str,
) -> str:
    labels = [row[0] if row else "" for row in rows]
    series_names = [columns[i] for i in measure_indices]
    colors = _PALETTES.get(palette, _PALETTES["tessallite"])

    width = 900
    height = {"sm": 260, "md": 340, "lg": 430}.get(size, 340)
    left = 64
    right = 64
    top = 32
    bottom = 70
    plot_w = width - left - right
    plot_h = height - top - bottom

    def x_at(idx: int) -> float:
        if len(rows) <= 1:
            return left + plot_w / 2
        return left + (plot_w * idx / (len(rows) - 1))

    paths: list[str] = []
    point_nodes: list[str] = []
    scale_notes: list[str] = []
    axis_groups: list[str] = []
    for si, col_idx in enumerate(measure_indices):
        values = [
            (_num(row[col_idx]) if len(row) > col_idx else None) or 0
            for row in rows
        ]
        axis_min, axis_max = _nice_axis_bounds(values)
        axis_span = axis_max - axis_min or 1
        color = colors[si % len(colors)]
        points = [
            (x_at(i), top + plot_h - (((v - axis_min) / axis_span) * plot_h))
            for i, v in enumerate(values)
        ]
        path_points = _svg_polyline(points)
        title = _esc(series_names[si])
        paths.append(
            f'<polyline data-series="{title}" data-axis-min="{_esc(_display_value(axis_min))}" '
            f'data-axis-max="{_esc(_display_value(axis_max))}" '
            f'points="{path_points}" fill="none" stroke="{color}" '
            'stroke-width="3" stroke-linejoin="round" stroke-linecap="round">'
            f'<title>{title} axis {_esc(_display_value(axis_min))} to {_esc(_display_value(axis_max))}</title>'
            '</polyline>'
        )
        for (x, y), raw, label in zip(points, values, labels):
            point_nodes.append(
                f'<circle data-series="{title}" data-label="{_esc(label)}" '
                f'data-value="{_esc(_display_value(raw))}" '
                f'cx="{x:.2f}" cy="{y:.2f}" r="3.25" fill="{color}">'
                f'<title>{title}: {_esc(_display_value(raw))}</title>'
                '</circle>'
            )
        scale_notes.append(f"{series_names[si]} {_display_value(axis_min)}-{_display_value(axis_max)}")
        if si < 2:
            axis_x = left if si == 0 else width - right
            text_anchor = "end" if si == 0 else "start"
            label_dx = -8 if si == 0 else 8
            tick_x2 = axis_x - 5 if si == 0 else axis_x + 5
            axis_groups.append(
                f'<line x1="{axis_x}" y1="{top}" x2="{axis_x}" y2="{top + plot_h}" '
                f'stroke="{color}" stroke-width="1.5" />'
            )
            axis_groups.append(
                f'<text x="{axis_x + label_dx}" y="{top - 8}" text-anchor="{text_anchor}" '
                f'font-size="11" font-weight="700" fill="{color}">{title}</text>'
            )
            for tick in range(5):
                ratio = tick / 4
                tick_value = axis_max - (axis_span * ratio)
                y = top + (plot_h * ratio)
                axis_groups.append(
                    f'<line x1="{axis_x}" y1="{y:.2f}" x2="{tick_x2}" y2="{y:.2f}" '
                    f'stroke="{color}" stroke-width="1" />'
                )
                axis_groups.append(
                    f'<text x="{axis_x + label_dx}" y="{y + 4:.2f}" text-anchor="{text_anchor}" '
                    f'font-size="10" fill="{color}">{_esc(_display_value(tick_value))}</text>'
                )

    grid = []
    for tick in range(5):
        y = top + (plot_h * tick / 4)
        grid.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{width - right}" y2="{y:.2f}" '
            'stroke="#d9e2dc" stroke-width="1" />'
        )

    x_labels = []
    for i, label in enumerate(labels):
        x = x_at(i)
        x_labels.append(
            f'<text x="{x:.2f}" y="{height - 24}" text-anchor="middle" '
            'font-size="11" font-weight="600" fill="#111">'
            f'{_esc(label)}</text>'
        )

    legend_items = []
    for si, name in enumerate(series_names):
        color = colors[si % len(colors)]
        x = left + (si * 210)
        legend_items.append(
            f'<g transform="translate({x},14)">'
            f'<rect width="12" height="12" fill="{color}" />'
            f'<text x="18" y="10" font-size="12" fill="#111">{_esc(name)}</text>'
            '</g>'
        )

    scale_text = "Dual-axis scale: " + "; ".join(scale_notes)
    return (
        '<div class="multi-metric-line-chart" data-renderer="multi_metric_line" '
        'style="width:100%;max-width:none;margin-bottom:8px">'
        f'{_chart_title(", ".join(series_names))}'
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{_esc(", ".join(series_names))} by {_esc(columns[0])}" '
        'style="display:block;width:100%;height:auto;min-height:260px;'
        'background:#f7fbf8;border:1px solid #c8d6cf;border-radius:4px">'
        f'<desc>{_esc(scale_text)}</desc>'
        f'{"".join(legend_items)}'
        f'{"".join(grid)}'
        f'{"".join(axis_groups)}'
        f'<line x1="{left}" y1="{top + plot_h}" x2="{width - right}" y2="{top + plot_h}" '
        'stroke="#111" stroke-width="1" />'
        f'{"".join(paths)}{"".join(point_nodes)}{"".join(x_labels)}'
        f'<text x="{left}" y="{height - 6}" font-size="10" fill="#555">{_esc(scale_text)}</text>'
        '</svg></div>'
    )


# ---------------------------------------------------------------------------
# Bar charts
# ---------------------------------------------------------------------------

def _render_column(
    columns: list[str],
    rows: list[list[Any]],
    palette: str,
    size: str,
) -> str:
    max_val = _max_measure(rows)
    wid = _wrapper_id()
    size_css = _SIZE_MAP.get(size, _SIZE_MAP["md"])
    body = []
    for row in rows:
        label = _esc(row[0])
        val = _num(row[1]) if len(row) > 1 else 0
        if val is None:
            val = 0
        norm = round(abs(val) / max_val, 4)
        neg = _negative_cell_style(val)
        body.append(
            f'<tr><th scope="row">{label}</th>'
            f'<td style="--size: {norm}{neg}"><span class="data">{_esc(_display_value(row[1] if len(row) > 1 else 0))}</span></td></tr>'
        )
    title = columns[1] if len(columns) > 1 else ""
    return (
        f'<div id="{wid}" style="{size_css}">'
        f'<style>{_palette_style(palette, wid)}</style>'
        f'{_chart_title(title)}'
        f'<table class="charts-css column show-labels show-data-on-hover'
        f' data-spacing-5 show-primary-axis show-4-secondary-axes">'
        f"<caption>{_esc(title)}</caption>"
        f"<tbody>{''.join(body)}</tbody>"
        f"</table></div>"
    )


def _render_bar(
    columns: list[str],
    rows: list[list[Any]],
    palette: str,
    size: str,
) -> str:
    max_val = _max_measure(rows)
    wid = _wrapper_id()
    size_css = _SIZE_MAP.get(size, _SIZE_MAP["md"])
    body = []
    for row in rows:
        label = _esc(row[0])
        val = _num(row[1]) if len(row) > 1 else 0
        if val is None:
            val = 0
        norm = round(abs(val) / max_val, 4)
        neg = _negative_cell_style(val)
        body.append(
            f'<tr><th scope="row">{label}</th>'
            f'<td style="--size: {norm}{neg}"><span class="data">{_esc(_display_value(row[1] if len(row) > 1 else 0))}</span></td></tr>'
        )
    title = columns[1] if len(columns) > 1 else ""
    return (
        f'<div id="{wid}" style="{size_css}">'
        f'<style>{_palette_style(palette, wid)}</style>'
        f'{_chart_title(title)}'
        f'<table class="charts-css bar show-labels show-data-on-hover'
        f' data-spacing-5 show-primary-axis show-4-secondary-axes">'
        f"<caption>{_esc(title)}</caption>"
        f"<tbody>{''.join(body)}</tbody>"
        f"</table></div>"
    )


# ---------------------------------------------------------------------------
# Line charts
# ---------------------------------------------------------------------------

def _render_line(
    columns: list[str],
    rows: list[list[Any]],
    palette: str,
    size: str,
) -> str:
    max_val = _max_measure(rows)
    wid = _wrapper_id()
    size_css = f'{_LINE_SIZE_MAP.get(size, _LINE_SIZE_MAP["md"])};margin-bottom:56px'
    body = []
    prev_norm = 0.0
    for i, row in enumerate(rows):
        label = _esc(row[0])
        val = (_num(row[1]) if len(row) > 1 else None) or 0
        norm = round(abs(val) / max_val, 4)
        start = prev_norm if i > 0 else norm
        body.append(
            f'<tr><th scope="row">{label}</th>'
            f'<td style="--start: {start}; --end: {norm}">'
            f'<span class="data">{_esc(_display_value(row[1] if len(row) > 1 else 0))}</span></td></tr>'
        )
        prev_norm = norm
    title = columns[1] if len(columns) > 1 else ""
    axis = _axis_labels_html([row[0] if row else "" for row in rows])
    return (
        f'<div id="{wid}" class="ts-line-chart" style="{size_css}">'
        f'<style>{_palette_style(palette, wid)} {_line_chart_hidden_label_style(wid)}</style>'
        f'{_chart_title(title)}'
        f'<table class="charts-css line show-labels show-data-on-hover'
        f' show-primary-axis show-4-secondary-axes">'
        f"<caption>{_esc(title)}</caption>"
        f"<tbody>{''.join(body)}</tbody>"
        f"</table>{axis}</div>"
    )


def _render_multi_line(
    columns: list[str],
    rows: list[list[Any]],
    palette: str,
    size: str,
) -> str:
    dates: list[str] = []
    series_names: list[str] = []
    pivot: dict[str, dict[str, float]] = OrderedDict()

    for row in rows:
        if len(row) < 3:
            continue
        date_key = str(row[0])
        series_key = str(row[1])
        val = _num(row[2]) or 0
        if date_key not in pivot:
            pivot[date_key] = {}
            dates.append(date_key)
        if series_key not in series_names:
            series_names.append(series_key)
        pivot[date_key][series_key] = val

    if not dates or not series_names:
        return ""

    all_vals = [v for d in pivot.values() for v in d.values()]
    max_val = max((abs(v) for v in all_vals), default=1) or 1

    wid = _wrapper_id()
    size_css = f'{_LINE_SIZE_MAP.get(size, _LINE_SIZE_MAP["md"])};margin-bottom:56px'
    prev_norms = {s: 0.0 for s in series_names}
    body = []

    for i, date_key in enumerate(dates):
        cells = f'<th scope="row">{_esc(date_key)}</th>'
        for s in series_names:
            val = pivot[date_key].get(s, 0)
            norm = round(abs(val) / max_val, 4)
            start = prev_norms[s] if i > 0 else norm
            cells += (
                f'<td style="--start: {start}; --end: {norm}">'
                f'<span class="data">{_esc(_display_value(val))}</span></td>'
            )
            prev_norms[s] = norm
        body.append(f"<tr>{cells}</tr>")

    title = columns[2] if len(columns) > 2 else ""
    legend = _legend_html(series_names, palette)
    axis = _axis_labels_html(dates)
    return (
        f'<div id="{wid}" class="ts-line-chart" style="{size_css}">'
        f'<style>{_palette_style(palette, wid)} {_line_chart_hidden_label_style(wid)}</style>'
        f'{_chart_title(title)}'
        f'{legend}'
        f'<table class="charts-css line multiple show-labels show-data-on-hover'
        f' show-primary-axis show-4-secondary-axes">'
        f"<caption>{_esc(title)}</caption>"
        f"<tbody>{''.join(body)}</tbody>"
        f"</table>{axis}</div>"
    )


def _render_multi_line_wide(
    columns: list[str],
    rows: list[list[Any]],
    palette: str,
    size: str,
) -> str:
    """Multi-series line from wide-format data: [dim, measure1, measure2, …]."""
    measure_indices = []
    for i in range(1, len(columns)):
        sample = [_num(row[i]) for row in rows[:10] if len(row) > i]
        if any(v is not None for v in sample):
            measure_indices.append(i)
    if not measure_indices:
        return ""

    series_names = [columns[i] for i in measure_indices]
    colors = _PALETTES.get(palette, _PALETTES["tessallite"])

    all_vals = [
        abs(n)
        for row in rows for i in measure_indices
        if len(row) > i and (n := _num(row[i])) is not None
    ]
    max_val = max(all_vals, default=1) or 1
    if _measure_scales_are_incompatible(rows, measure_indices):
        return _render_multi_metric_line_svg(columns, rows, measure_indices, palette, size)

    wid = _wrapper_id()
    size_css = f'{_LINE_SIZE_MAP.get(size, _LINE_SIZE_MAP["md"])};margin-bottom:56px'
    prev_norms = {i: 0.0 for i in measure_indices}
    body = []

    for ri, row in enumerate(rows):
        label = _esc(row[0])
        cells = f'<th scope="row">{label}</th>'
        for ci, col_idx in enumerate(measure_indices):
            val = (_num(row[col_idx]) if len(row) > col_idx else None) or 0
            norm = round(abs(val) / max_val, 4)
            start = prev_norms[col_idx] if ri > 0 else norm
            cells += (
                f'<td style="--start: {start}; --end: {norm}; '
                f'--color: {colors[ci % len(colors)]}">'
                f'<span class="data">{_esc(_display_value(val))}</span></td>'
            )
            prev_norms[col_idx] = norm
        body.append(f"<tr>{cells}</tr>")

    title = ", ".join(series_names)
    legend = _legend_html(series_names, palette)
    axis = _axis_labels_html([row[0] if row else "" for row in rows])
    return (
        f'<div id="{wid}" class="ts-line-chart" style="{size_css}">'
        f'<style>{_palette_style(palette, wid)} {_line_chart_hidden_label_style(wid)}</style>'
        f'{_chart_title(title)}'
        f'{legend}'
        f'<table class="charts-css line multiple show-labels show-data-on-hover'
        f' show-primary-axis show-4-secondary-axes">'
        f"<caption>{_esc(title)}</caption>"
        f"<tbody>{''.join(body)}</tbody>"
        f"</table>{axis}</div>"
    )


# ---------------------------------------------------------------------------
# Pie chart
# ---------------------------------------------------------------------------

def _render_pie(
    columns: list[str],
    rows: list[list[Any]],
    palette: str,
    size: str,
) -> str:
    values = [abs(n) for r in rows
              if len(r) > 1 and (n := _num(r[1])) is not None]
    total = sum(values) or 1
    colors = _PALETTES.get(palette, _PALETTES["tessallite"])

    wid = _wrapper_id()
    size_css = _SIZE_MAP.get(size, _SIZE_MAP["md"])
    body = []
    cumulative = 0.0

    for i, row in enumerate(rows):
        label = _esc(row[0])
        raw = _num(row[1]) if len(row) > 1 else None
        val = abs(raw) if raw is not None else 0
        fraction = val / total
        start = round(cumulative, 4)
        end = round(cumulative + fraction, 4)
        color = colors[i % len(colors)]
        body.append(
            f'<tr><th scope="row">{label}</th>'
            f'<td style="--start: {start}; --end: {end}; --color: {color}">'
            f'<span class="data">{_esc(_display_value(val))}</span></td></tr>'
        )
        cumulative += fraction

    title = columns[1] if len(columns) > 1 else ""
    dim_labels = [str(r[0]) for r in rows]
    legend = _legend_html(dim_labels, palette)
    return (
        f'<div id="{wid}" style="{size_css}">'
        f'<style>{_palette_style(palette, wid)}</style>'
        f'{_chart_title(title)}'
        f'<table class="charts-css pie">'
        f"<caption>{_esc(title)}</caption>"
        f"<tbody>{''.join(body)}</tbody>"
        f"</table>"
        f'{legend}'
        f"</div>"
    )


# ---------------------------------------------------------------------------
# Multi-measure bar charts (grouped / stacked)
# ---------------------------------------------------------------------------

def _render_multi_column(
    columns: list[str],
    rows: list[list[Any]],
    palette: str,
    size: str,
    stacked: bool = False,
) -> str:
    measure_indices = []
    for i in range(1, len(columns)):
        sample = [_num(row[i]) for row in rows[:10] if len(row) > i]
        if any(v is not None for v in sample):
            measure_indices.append(i)
    if not measure_indices:
        return ""

    measure_names = [columns[i] for i in measure_indices]
    if not stacked and _measure_scales_are_incompatible(rows, measure_indices):
        return _render_split_measure_columns(columns, rows, measure_indices, palette, size)

    if stacked:
        max_val = max(
            (sum(abs(n) for i in measure_indices
                 if len(row) > i and (n := _num(row[i])) is not None)
             for row in rows),
            default=1,
        ) or 1
    else:
        all_vals = [
            abs(n)
            for row in rows for i in measure_indices
            if len(row) > i and (n := _num(row[i])) is not None
        ]
        max_val = max(all_vals, default=1) or 1

    colors = _PALETTES.get(palette, _PALETTES["tessallite"])
    wid = _wrapper_id()
    size_css = _SIZE_MAP.get(size, _SIZE_MAP["md"])
    body = []

    for row in rows:
        label = _esc(row[0])
        cells = f'<th scope="row">{label}</th>'
        for ci, col_idx in enumerate(measure_indices):
            val = (_num(row[col_idx]) if len(row) > col_idx else None) or 0
            norm = round(abs(val) / max_val, 4)
            cells += (
                f'<td style="--size: {norm}; --color: {colors[ci % len(colors)]}">'
                f'<span class="data">{_esc(_display_value(val))}</span></td>'
            )
        body.append(f"<tr>{cells}</tr>")

    modifier = "multiple stacked" if stacked else "multiple"
    title = ", ".join(measure_names)
    legend = _legend_html(measure_names, palette)
    return (
        f'<div id="{wid}" style="{size_css}">'
        f'<style>{_palette_style(palette, wid)}</style>'
        f'{_chart_title(title)}'
        f'{legend}'
        f'<table class="charts-css column {modifier} show-labels show-data-on-hover'
        f' data-spacing-5 show-primary-axis show-4-secondary-axes">'
        f"<caption>{_esc(title)}</caption>"
        f"<tbody>{''.join(body)}</tbody>"
        f"</table></div>"
    )


def _measure_scales_are_incompatible(
    rows: list[list[Any]],
    measure_indices: list[int],
) -> bool:
    maxima: list[float] = []
    for col_idx in measure_indices:
        vals = [
            abs(n)
            for row in rows
            if len(row) > col_idx and (n := _num(row[col_idx])) is not None and n != 0
        ]
        if vals:
            maxima.append(max(vals))
    if len(maxima) < 2:
        return False
    smallest = min(maxima)
    largest = max(maxima)
    return smallest > 0 and largest / smallest >= 100


def _render_split_measure_columns(
    columns: list[str],
    rows: list[list[Any]],
    measure_indices: list[int],
    palette: str,
    size: str,
) -> str:
    chart_parts = []
    for col_idx in measure_indices:
        measure_rows = [
            [row[0] if row else "", row[col_idx] if len(row) > col_idx else None]
            for row in rows
        ]
        chart_parts.append(_render_column([columns[0], columns[col_idx]], measure_rows, palette, size))
    return (
        '<div class="multi-measure-split-charts" '
        'data-renderer="split_grouped_bar" '
        'style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px">'
        + "".join(chart_parts)
        + "</div>"
    )


def _is_long_form_stack(columns: list[str], rows: list[list[Any]]) -> bool:
    if len(columns) < 3 or not rows:
        return False
    first_rows = rows[:10]
    primary_numeric = any(_num(row[0]) is not None for row in first_rows if len(row) > 0)
    series_numeric = any(_num(row[1]) is not None for row in first_rows if len(row) > 1)
    values = [_num(row[2]) for row in first_rows if len(row) > 2]
    return not primary_numeric and not series_numeric and any(value is not None for value in values)


def _long_form_pivot(rows: list[list[Any]]) -> tuple[list[str], list[str], dict[str, dict[str, float]]]:
    primary_labels: list[str] = []
    series_labels: list[str] = []
    pivot: dict[str, dict[str, float]] = OrderedDict()
    for row in rows:
        if len(row) < 3:
            continue
        primary = str(row[0])
        series = str(row[1])
        value = _num(row[2]) or 0
        if primary not in pivot:
            pivot[primary] = {}
            primary_labels.append(primary)
        if series not in series_labels:
            series_labels.append(series)
        pivot[primary][series] = value
    return primary_labels, series_labels, pivot


def _render_stacked_long(
    columns: list[str],
    rows: list[list[Any]],
    palette: str,
    size: str,
) -> str:
    primary_labels, series_labels, pivot = _long_form_pivot(rows)
    if not primary_labels or not series_labels:
        return ""

    max_val = max(
        (sum(abs(pivot[primary].get(series, 0)) for series in series_labels) for primary in primary_labels),
        default=1,
    ) or 1
    colors = _PALETTES.get(palette, _PALETTES["tessallite"])
    wid = _wrapper_id()
    size_css = _SIZE_MAP.get(size, _SIZE_MAP["md"])
    body = []
    for primary in primary_labels:
        cells = f'<th scope="row">{_esc(primary)}</th>'
        for ci, series in enumerate(series_labels):
            val = pivot[primary].get(series, 0)
            norm = round(abs(val) / max_val, 4)
            cells += (
                f'<td data-series="{_esc(series)}" style="--size: {norm}; --color: {colors[ci % len(colors)]}">'
                f'<span class="data">{_esc(_display_value(val))}</span></td>'
            )
        body.append(f"<tr>{cells}</tr>")

    title = columns[2] if len(columns) > 2 else ""
    legend = _legend_html(series_labels, palette)
    return (
        f'<div id="{wid}" style="{size_css}">'
        f'<style>{_palette_style(palette, wid)}</style>'
        f'{_chart_title(title)}'
        f'{legend}'
        f'<table class="charts-css column multiple stacked show-labels show-data-on-hover'
        f' data-spacing-5 show-primary-axis show-4-secondary-axes">'
        f"<caption>{_esc(title)}</caption>"
        f"<tbody>{''.join(body)}</tbody>"
        f"</table></div>"
    )


def _build_pivot_table(columns: list[str], rows: list[list[Any]]) -> str:
    if not _is_long_form_stack(columns, rows):
        return _build_table(columns, rows)
    primary_labels, series_labels, pivot = _long_form_pivot(rows)
    primary_title = columns[0]
    header = f"<th>{_esc(primary_title)}</th>" + "".join(f"<th>{_esc(series)}</th>" for series in series_labels)
    body = []
    for primary in primary_labels:
        cells = f"<td>{_esc(primary)}</td>"
        cells += "".join(
            f"<td>{_esc(_display_value(pivot[primary].get(series, '')))}</td>"
            for series in series_labels
        )
        body.append(f"<tr>{cells}</tr>")
    return (
        '<table class="matrix-pivot-table" style="border-collapse:collapse;font-size:12px;margin-top:8px;width:100%">'
        f"<thead><tr>{header}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody>"
        "</table>"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _ensure_date_first(
    columns: list[str],
    rows: list[list[Any]],
) -> tuple[list[str], list[list[Any]]]:
    """Reorder columns so the date column is at index 0 for multi_line.

    multi_line expects [date, series, measure]. If the date column is not
    at index 0, swap it into position.
    """
    from src.charts.selector import _looks_like_date, _temporal_int_column

    if len(columns) < 3:
        return columns, rows
    date_idx: int | None = None
    for idx in range(len(columns)):
        col_values = [row[idx] for row in rows[:10] if idx < len(row)]
        if _looks_like_date(col_values) or _temporal_int_column(columns[idx], col_values):
            date_idx = idx
            break

    if date_idx is None or date_idx == 0:
        return columns, rows

    order = list(range(len(columns)))
    order.remove(date_idx)
    order.insert(0, date_idx)
    new_columns = [columns[i] for i in order]
    new_rows = [[row[i] if i < len(row) else None for i in order] for row in rows]
    return new_columns, new_rows


def _steps_compatible(step_results: list[dict]) -> bool:
    if not step_results:
        return True
    first_cols = set(step_results[0].get("columns", []))
    return all(set(s.get("columns", [])) == first_cols for s in step_results)


def render_compound_result(
    step_results: list[dict],
    computed: dict,
    include_step_tables: bool = True,
) -> str:
    label = _esc(computed.get("label", ""))
    value = computed.get("value")
    if isinstance(value, dict):
        kpi_parts = []
        for sub_label, sub_val in value.items():
            kpi_parts.append(
                f'<div class="compound-kpi">'
                f'<div style="font-size:13px;font-weight:600;margin-bottom:4px;color:#444">'
                f'{_esc(sub_label)}</div>'
                f'{_render_kpi(sub_val)}'
                f'</div>'
            )
        kpi_html = "".join(kpi_parts)
    elif value is not None:
        kpi_html = (
            f'<div class="compound-kpi">'
            f'<div style="font-size:13px;font-weight:600;margin-bottom:4px;color:#444">{label}</div>'
            f'{_render_kpi(value)}'
            f'</div>'
        )
    else:
        kpi_html = ""

    if not step_results:
        return f'<div class="compound-result">{kpi_html}</div>'

    if _steps_compatible(step_results):
        columns = step_results[0].get("columns", [])
        header_cells = "<th>Step</th>" + "".join(f"<th>{_esc(c)}</th>" for c in columns)
        body_rows = []
        for s in step_results:
            row = s.get("rows", [{}])[0] if s.get("rows") else {}
            cells = f"<td>{_esc(s['name'])}</td>"
            cells += "".join(
                f"<td>{_esc(_display_value(row.get(c, '')))}</td>"
                for c in columns
            )
            body_rows.append(f"<tr>{cells}</tr>")
        computed_cells = f'<td>{label}</td>'
        if isinstance(value, dict):
            formatted = " / ".join(
                f"{_esc(_display_value(v))}" for v in value.values()
            )
            if len(columns) > 0:
                computed_cells += f'<td colspan="{len(columns)}">{formatted}</td>'
        elif len(columns) > 0:
            computed_cells += f'<td colspan="{len(columns)}">{_esc(_display_value(value))}</td>'
        body_rows.append(f'<tr class="compound-computed">{computed_cells}</tr>')
        table = (
            '<table class="compound-summary" style="border-collapse:collapse;font-size:12px;margin-top:8px;width:100%">'
            f"<thead><tr>{header_cells}</tr></thead>"
            f"<tbody>{''.join(body_rows)}</tbody>"
            "</table>"
        )
        return f'<div class="compound-result">{kpi_html}{table}</div>'

    parts = [kpi_html]
    for s in step_results:
        name = _esc(s.get("name", ""))
        rows = s.get("rows", [])
        columns = s.get("columns", [])
        row_count = len(rows)
        if include_step_tables and columns and rows:
            row_lists = [[r.get(c) for c in columns] for r in rows]
            table_html = _build_table(columns, row_lists)
        else:
            table_html = ""
        parts.append(
            f'<details class="compound-step">'
            f"<summary>Step: {name} ({row_count} row{'s' if row_count != 1 else ''})</summary>"
            f"{table_html}"
            f"</details>"
        )
    return f'<div class="compound-result">{"".join(parts)}</div>'


def _collapse_dims(
    columns: list[str],
    rows: list[list[Any]],
) -> tuple[list[str], list[list[Any]]]:
    """Collapse multiple dimension columns into a single label column.

    Single-series renderers (bar, h_bar, line, pie) expect [label, value].
    When a result has 3+ columns with multiple leading non-numeric dims,
    join them into one label so the renderer gets the right shape.
    """
    if len(columns) <= 2:
        return columns, rows

    measure_indices = []
    for idx in range(len(columns)):
        sample = [row[idx] for row in rows[:10] if idx < len(row)]
        nums = [_num(v) for v in sample if v is not None]
        if nums and all(n is not None for n in nums):
            measure_indices.append(idx)

    dim_indices = [i for i in range(len(columns)) if i not in measure_indices]

    if len(dim_indices) <= 1 or len(measure_indices) == 0:
        return columns, rows

    new_label = " / ".join(columns[i] for i in dim_indices)
    new_columns = [new_label] + [columns[i] for i in measure_indices]
    new_rows = []
    for row in rows:
        label = " - ".join(str(row[i]) if i < len(row) else "" for i in dim_indices)
        vals = [row[i] if i < len(row) else None for i in measure_indices]
        new_rows.append([label] + vals)
    return new_columns, new_rows


def render_chart(
    chart_type: str | None,
    columns: list[str],
    rows: list[list[Any]],
    palette: str = "default",
    size: str = "md",
    include_table: bool = True,
) -> str:
    if not chart_type or not rows or not columns:
        return ""

    try:
        if chart_type == "kpi":
            if len(columns) > 1 and len(rows) == 1:
                chart_html = _render_multi_kpi(columns, rows[0])
            else:
                value = rows[0][-1] if rows and rows[0] else ""
                chart_html = _render_kpi(value)
            table_html = _build_table(columns, rows) if include_table else ""
            return chart_html + table_html

        elif chart_type == "bar":
            c, r = _collapse_dims(columns, rows)
            chart_html = _render_column(c, r, palette, size)

        elif chart_type == "h_bar":
            c, r = _collapse_dims(columns, rows)
            chart_html = _render_bar(c, r, palette, size)

        elif chart_type == "line":
            c, r = _collapse_dims(columns, rows)
            chart_html = _render_line(c, r, palette, size)

        elif chart_type == "multi_line":
            columns, rows = _ensure_date_first(columns, rows)
            chart_html = _render_multi_line(columns, rows, palette, size)

        elif chart_type == "multi_line_wide":
            chart_html = _render_multi_line_wide(columns, rows, palette, size)

        elif chart_type == "pie":
            c, r = _collapse_dims(columns, rows)
            chart_html = _render_pie(c, r, palette, size)

        elif chart_type == "grouped_bar":
            chart_html = _render_multi_column(columns, rows, palette, size, stacked=False)

        elif chart_type == "stacked_bar":
            if _is_long_form_stack(columns, rows):
                chart_html = _render_stacked_long(columns, rows, palette, size)
            else:
                chart_html = _render_multi_column(columns, rows, palette, size, stacked=True)

        else:
            return ""

    except Exception:
        return ""

    table_html = _build_pivot_table(columns, rows) if include_table and chart_type == "stacked_bar" else _build_table(columns, rows) if include_table else ""
    return chart_html + table_html
