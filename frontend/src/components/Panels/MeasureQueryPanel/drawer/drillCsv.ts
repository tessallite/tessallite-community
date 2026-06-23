import type { DrillThroughResponse } from "../../../../api/types";
import { renderDimValue } from "../pivot";
import { safeFilename } from "../export/download";

// F-019-20: the drill CSV export contains only the rows on the current page,
// not the full drill-through result. The filename says so explicitly so users
// do not mistake a page export for the complete set behind the clicked cell.
export function drillCurrentPageCsvFilename(measureDisplayName: string): string {
  return `${safeFilename(`${measureDisplayName}-drill-current-page`)}.csv`;
}

function csvEscape(s: string): string {
  if (/[",\r\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

export function drillRowsToCsv(
  result: DrillThroughResponse,
  visibleColumns: string[],
): string {
  const cols = result.columns.filter((c) => visibleColumns.includes(c));
  const header = cols.map((c) => csvEscape(c)).join(",");
  const body = result.rows
    .map((row) =>
      cols
        .map((c) => {
          const v = row[c];
          return v === null || v === undefined ? "" : csvEscape(renderDimValue(v));
        })
        .join(","),
    )
    .join("\r\n");
  return header + "\r\n" + body + (body.length > 0 ? "\r\n" : "");
}
