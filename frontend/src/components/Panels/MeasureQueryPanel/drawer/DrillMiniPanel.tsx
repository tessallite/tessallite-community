import { useEffect, useState } from "react";
import { useT } from "../../../../i18n";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import { queryRouterApiClient } from "../../../../api/client";
import type { Dimension, DrillThroughFilter, DrillThroughResponse, Measure } from "../../../../api/types";
import { renderDimValue } from "../pivot";
import type { CellCoord } from "../types";

type Props = {
  measure: Measure;
  coord: CellCoord;
  rowDims: Dimension[];
  colDims: Dimension[];
  personaId?: string | null;
  // F-019-03: active-slicer predicates so the decomposed drill reconciles
  // with the clicked calculated-measure cell.
  filters?: DrillThroughFilter[];
};

const PAGE_SIZE = 50;

function extractError(err: unknown, failedMsg: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (detail) {
    if (typeof detail === "string") return detail;
    if (typeof detail === "object" && detail !== null) {
      const d = detail as Record<string, unknown>;
      if (typeof d.message === "string") return d.message;
      if (typeof d.detail === "string") return d.detail;
      return JSON.stringify(detail);
    }
  }
  if (err instanceof Error) return err.message;
  return failedMsg;
}

export default function DrillMiniPanel({ measure, coord, rowDims, colDims, personaId, filters }: Props) {
  const t = useT();
  const [result, setResult] = useState<DrillThroughResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([]);

  async function load(cursor: string | null) {
    const groupingLevels: { column: string; op: "eq"; value: unknown }[] = [];
    rowDims.forEach((d, i) => {
      groupingLevels.push({ column: d.name, op: "eq", value: coord.rowValues[i] });
    });
    colDims.forEach((d, i) => {
      groupingLevels.push({ column: d.name, op: "eq", value: coord.colValues[i] });
    });
    setLoading(true);
    setError(null);
    try {
      const r = await queryRouterApiClient.drillThrough(
        measure.id,
        {
          grouping_levels: groupingLevels,
          ...(filters && filters.length > 0 ? { filters } : {}),
          limit: PAGE_SIZE,
          ...(cursor ? { cursor } : {}),
        },
        personaId,
      );
      setResult(r);
    } catch (err) {
      setError(extractError(err, t("drillMini.failed")));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    setCursorStack([]);
    setResult(null);
    void load(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [measure.id, coord.rowKey.join("|"), coord.colKey.join("|"), personaId]);

  async function handleNext() {
    if (!result?.page.next_cursor) return;
    const current = result.page.cursor || null;
    setCursorStack((s) => [...s, current]);
    await load(result.page.next_cursor);
  }

  async function handlePrev() {
    if (cursorStack.length === 0) return;
    const stack = [...cursorStack];
    const prev = stack.pop() ?? null;
    setCursorStack(stack);
    await load(prev);
  }

  const hasNext = Boolean(result?.page.has_more);
  const hasPrev = cursorStack.length > 0;

  return (
    <Paper variant="outlined" sx={{ p: 1.5, display: "flex", flexDirection: "column", gap: 1 }}>
      <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap">
        <Typography variant="subtitle2">{measure.display_name || measure.name}</Typography>
        <Chip size="small" label={measure.measure_type} variant="outlined" />
        {result && (
          <Typography variant="caption" color="text.secondary">
            {result.route_type && `${result.route_type} · `}{t("drillMini.rowsOnPage", { count: String(result.rows.length) })}
            {result.execution_ms > 0 && ` · ${result.execution_ms} ms`}
          </Typography>
        )}
      </Stack>

      {error && <Alert severity="error">{error}</Alert>}
      {loading && <CircularProgress size={18} />}

      {result && !loading && (
        <>
          <TableContainer sx={{ maxHeight: 260, overflow: "auto" }}>
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  {result.columns.map((col) => (
                    <TableCell key={col} sx={{ fontWeight: 600 }}>
                      {col}
                    </TableCell>
                  ))}
                </TableRow>
              </TableHead>
              <TableBody>
                {result.rows.map((row, i) => (
                  <TableRow key={i}>
                    {result.columns.map((col) => (
                      <TableCell key={col}>{renderDimValue(row[col])}</TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>

          <Stack direction="row" gap={1} alignItems="center">
            <Button size="small" variant="outlined" disabled={loading || !hasPrev} onClick={handlePrev}>
              {t("drillMini.prev")}
            </Button>
            <Button size="small" variant="outlined" disabled={loading || !hasNext} onClick={handleNext}>
              {t("drillMini.next")}
            </Button>
            <Box sx={{ flexGrow: 1 }} />
            <Typography variant="caption" color="text.secondary">
              {t("drillMini.pageSize", { size: String(PAGE_SIZE) })}
            </Typography>
          </Stack>
        </>
      )}
    </Paper>
  );
}
