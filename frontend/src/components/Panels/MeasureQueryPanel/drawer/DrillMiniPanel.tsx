import { useEffect, useState } from "react";
import { useT } from "../../../../i18n";
import {
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
import { rowSecurityDeniedAll } from "../../../../utils/rowSecurity";
import type { Dimension, DrillThroughFilter, DrillThroughResponse, Measure } from "../../../../api/types";
import { renderDimValue } from "../pivot";
import type { CellCoord } from "../types";
import PivotErrorAlert from "../PivotErrorAlert";
import { toPivotError, type PivotPanelError } from "../pivotErrors";

type Props = {
  measure: Measure;
  coord: CellCoord;
  rowDims: Dimension[];
  colDims: Dimension[];
  personaId?: string | null;
  // F-019-03: active-slicer predicates so the decomposed drill reconciles
  // with the clicked calculated-measure cell.
  filters?: DrillThroughFilter[];
  // Bug-6278: when Force Live is on, decomposition drills must also pin to
  // source data so they are not silently served from an aggregate/pocket.
  forceLive?: boolean;
};

const PAGE_SIZE = 50;

export default function DrillMiniPanel({ measure, coord, rowDims, colDims, personaId, filters, forceLive }: Props) {
  const t = useT();
  const [result, setResult] = useState<DrillThroughResponse | null>(null);
  const [loading, setLoading] = useState(false);
  // Bug-8182 (review B4): a drill failure leads with a friendly message and keeps
  // raw backend/transport text behind the collapsed accordion. A calc-measure
  // drill is a QUERY, so the friendly message is the generic drill-failed one —
  // never a pivot-config-invalid message (toPivotError's membership gate ensures
  // a query-router code is not mislabeled).
  const [error, setError] = useState<PivotPanelError | null>(null);
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([]);

  async function load(cursor: string | null) {
    const groupingLevels: { column: string; op: "eq"; value: unknown }[] = [];
    rowDims.slice(0, coord.rowValues.length).forEach((d, i) => {
      groupingLevels.push({ column: d.name, op: "eq", value: coord.rowValues[i] });
    });
    colDims.slice(0, coord.colValues.length).forEach((d, i) => {
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
          ...(forceLive ? { force_route: "source" as const } : {}),
        },
        personaId,
      );
      // Bug-8453 / R5 finding F2: an RLS deny-all returns zero detail rows.
      // Rendering that as an empty drill grid tells the analyst the cell has
      // no underlying detail, which is a claim about the data rather than
      // about their access.
      if (rowSecurityDeniedAll(r)) {
        setResult(null);
        setError({ message: t("query.rowSecurityDeniedBody") });
        return;
      }
      setResult(r);
    } catch (err) {
      setError(toPivotError(err, t, t("drillMini.failed")));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    setCursorStack([]);
    setResult(null);
    void load(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [measure.id, coord.rowKey.join("|"), coord.colKey.join("|"), personaId, forceLive]);

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

      {error && <PivotErrorAlert error={error} />}
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
