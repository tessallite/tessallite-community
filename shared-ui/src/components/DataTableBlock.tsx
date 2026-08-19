import { useMemo, useState, useCallback } from "react";
import {
  Box,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TablePagination,
  TableSortLabel,
  IconButton,
  Tooltip,
  Typography,
} from "@mui/material";
import { ContentCopy } from "@mui/icons-material";
import { ErrorBoundary } from "./ErrorBoundary";
import { useChatContext } from "../providers/ChatProvider";

interface DataTableBlockProps {
  rows: Record<string, unknown>[];
  maxRows?: number;
}

type SortDir = "asc" | "desc";

type TranslateFn = (key: string, params?: Record<string, string | number>) => string;

// Bug-6519: columns whose integer values are identifiers or years must NOT get
// digit grouping (thousands separators): a year "2024" must render as "2024",
// not "2,024", and an id "10001" as "10001", not "10,001". The distinction is a
// property of the column's *semantics*, not the value — a count that happens to
// equal 2024 is still a quantity and should group — so detection is by column
// name (token match against known identifier/year names), never by magnitude.
//
// Identifier tokens may appear anywhere in the name (e.g. "customer_id",
// "id_customer"). Temporal tokens (year/yr/fy) only ungroup when they are the
// whole name or its trailing segment ("year", "fiscal_year", "order_year"): a
// leading temporal token on a compound name is almost always a measure whose
// values are quantities, not years — "year_revenue" and "fy_2024" hold amounts
// and must keep grouping.
const UNGROUPED_ID_RE =
  /(?:^|[^a-z])(?:id|ids|uuid|guid|zip|zipcode|postcode|postal|sku|isbn|ssn)(?:[^a-z]|$)/i;
const UNGROUPED_YEAR_RE = /(?:^|_)(?:year|yr|fy)$/i;

function isUngroupedColumn(col: string): boolean {
  return UNGROUPED_ID_RE.test(col) || UNGROUPED_YEAR_RE.test(col);
}

function formatNumeric(num: number, grouped: boolean): string {
  // Grouping is only suppressed for integers (the year/id case). Decimals are
  // never identifiers, so they keep locale-default grouping.
  if (Number.isInteger(num)) {
    return num.toLocaleString(undefined, { useGrouping: grouped });
  }
  return num.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatCell(val: unknown, t: TranslateFn, grouped: boolean): string {
  if (typeof val === "number") {
    return formatNumeric(val, grouped);
  }
  if (typeof val === "boolean") return val ? t("dataTable.yes") : t("dataTable.no");
  const str = String(val);
  // Bug-6519 follow-up: never re-parse a STRING value in an identifier/year
  // column. Coercing via Number() silently drops leading zeros (a zip "07030"
  // becomes 7030) and loses precision on bigint-as-string ids beyond
  // MAX_SAFE_INTEGER — Postgres bigints are often serialised as JSON strings
  // precisely to preserve those digits. Only numeric-looking strings in genuine
  // quantity columns (grouped) are reformatted for thousands separators.
  if (
    grouped &&
    /^-?(?:\d+(\.\d+)?|\d*\.?\d+e[+-]?\d+)$/i.test(str) &&
    str.length > 3
  ) {
    const num = Number(str);
    if (Number.isFinite(num)) {
      return formatNumeric(num, grouped);
    }
  }
  return str;
}

function prettifyHeader(col: string): string {
  return col
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function DataTableBlock({
  rows,
  maxRows = 5000,
}: DataTableBlockProps) {
  const { t } = useChatContext();
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  const [sortCol, setSortCol] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>("asc");
  const [copied, setCopied] = useState(false);

  const columns = useMemo(() => {
    if (rows.length === 0) return [];
    return Object.keys(rows[0]!);
  }, [rows]);

  // Bug-6519: per-column flag — true means integer values in this column keep
  // digit grouping; false means suppress it (year/identifier columns).
  const groupedByColumn = useMemo(() => {
    const map: Record<string, boolean> = {};
    for (const col of columns) map[col] = !isUngroupedColumn(col);
    return map;
  }, [columns]);

  const sorted = useMemo(() => {
    if (!sortCol) return rows;
    return [...rows].sort((a, b) => {
      const va = a[sortCol];
      const vb = b[sortCol];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      const cmp =
        typeof va === "number" && typeof vb === "number"
          ? va - vb
          : String(va).localeCompare(String(vb));
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [rows, sortCol, sortDir]);

  const paginated = useMemo(
    () => sorted.slice(page * rowsPerPage, page * rowsPerPage + rowsPerPage),
    [sorted, page, rowsPerPage],
  );

  const handleSort = useCallback(
    (col: string) => {
      if (sortCol === col) {
        setSortDir((d) => (d === "asc" ? "desc" : "asc"));
      } else {
        setSortCol(col);
        setSortDir("asc");
      }
    },
    [sortCol],
  );

  const handleCopy = useCallback(() => {
    const header = columns.join("\t");
    const body = sorted
      .map((r) => columns.map((c) => String(r[c] ?? "")).join("\t"))
      .join("\n");
    navigator.clipboard.writeText(header + "\n" + body).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [columns, sorted]);

  if (rows.length === 0) return null;

  if (rows.length > maxRows) {
    return (
      <Box sx={{ mt: 1, p: 1.5, bgcolor: "action.hover", borderRadius: 1 }}>
        <Typography variant="body2" color="text.secondary">
          {t("dataTable.exceedsLimit", {
            count: rows.length.toLocaleString(),
          })}
        </Typography>
      </Box>
    );
  }

  return (
    <ErrorBoundary>
      <Box sx={{ mt: 1, minWidth: 0, maxWidth: "100%" }}>
        <Box sx={{ display: "flex", justifyContent: "flex-end", mb: 0.5 }}>
          <Tooltip title={copied ? t("dataTable.copied") : t("dataTable.copy")}>
            <IconButton size="small" onClick={handleCopy}>
              <ContentCopy sx={{ fontSize: 14 }} />
            </IconButton>
          </Tooltip>
        </Box>
        <TableContainer
          sx={{
            maxHeight: 400,
            maxWidth: "100%",
            border: 1,
            borderColor: "divider",
            borderRadius: 1,
          }}
        >
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                {columns.map((col) => (
                  <TableCell
                    key={col}
                    sx={{ fontWeight: 600, whiteSpace: "nowrap", fontSize: 12 }}
                  >
                    <TableSortLabel
                      active={sortCol === col}
                      direction={sortCol === col ? sortDir : "asc"}
                      onClick={() => handleSort(col)}
                    >
                      {prettifyHeader(col)}
                    </TableSortLabel>
                  </TableCell>
                ))}
              </TableRow>
            </TableHead>
            <TableBody>
              {paginated.map((row, i) => (
                <TableRow key={i} hover>
                  {columns.map((col) => {
                    const val = row[col];
                    const display =
                      val == null
                        ? "—"
                        : formatCell(val, t, groupedByColumn[col] ?? true);
                    return (
                      <TableCell
                        key={col}
                        sx={{
                          fontSize: 12,
                          maxWidth: 240,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {display}
                      </TableCell>
                    );
                  })}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
        <TablePagination
          component="div"
          count={sorted.length}
          page={page}
          onPageChange={(_, p) => setPage(p)}
          rowsPerPage={rowsPerPage}
          onRowsPerPageChange={(e) => {
            setRowsPerPage(parseInt(e.target.value, 10));
            setPage(0);
          }}
          rowsPerPageOptions={[10, 25, 50, 100]}
          sx={{ borderTop: 1, borderColor: "divider" }}
        />
      </Box>
    </ErrorBoundary>
  );
}
