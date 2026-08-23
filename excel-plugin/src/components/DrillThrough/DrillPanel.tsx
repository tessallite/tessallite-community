import { useState, useCallback, useEffect } from 'react';
import {
  Box, Typography, IconButton, Button, CircularProgress,
  Table, TableHead, TableBody, TableRow, TableCell,
} from '@mui/material';
import { Close, ContentCopy, NavigateNext } from '@mui/icons-material';
import { tokens } from '../../theme';
import { getDrillOptions, drillThrough } from '../../api/queryRouter';
import { rowSecurityDeniedAll } from '../../utils/rowSecurity';
import { getDrillThroughSet } from '../../api/modelService';
import { ApiError } from '../../api/client';
import DrillPathPicker from './DrillPathPicker';
import type { DrillOption, DrillThroughResponse, DrillThroughSet } from '../../types/tessallite';
import { strings, templates } from '../../i18n/strings';

interface DrillPanelProps {
  open: boolean;
  onClose: () => void;
  measureId: string;
  measureName: string;
  context: Record<string, unknown>;
  projectId?: string;
  modelId?: string;
  onInsertSheet: (headers: string[], rows: (string | number)[][]) => void;
}

const DRILL_PAGE_SIZE = 50;

interface BreadcrumbSegment {
  label: string;
  onClick?: () => void;
}

export default function DrillPanel({
  open, onClose, measureId, measureName, context, projectId, modelId, onInsertSheet,
}: DrillPanelProps) {
  const [options, setOptions] = useState<DrillOption[]>([]);
  const [selectedPath, setSelectedPath] = useState('');
  const [result, setResult] = useState<DrillThroughResponse | null>(null);
  const [loading, setLoading] = useState(false);
  // Bug-8453 / R5 finding F2: row security denied every detail row.
  const [rowSecurityDenied, setRowSecurityDenied] = useState(false);
  const [page, setPage] = useState(0);
  const [allRows, setAllRows] = useState<Record<string, unknown>[]>([]);
  const [nextCursor, setNextCursor] = useState<string | undefined>(undefined);
  const [hasMore, setHasMore] = useState(false);
  const [optionsLoaded, setOptionsLoaded] = useState(false);
  const [drillSet, setDrillSet] = useState<DrillThroughSet | null>(null);
  const [accessDenied, setAccessDenied] = useState(false);

  useEffect(() => {
    if (!open || !measureId) return;
    let cancelled = false;
    setLoading(true);
    setAccessDenied(false);

    const loadOptions = getDrillOptions(measureId, context)
      .then(opts => {
        if (!cancelled) { setOptions(opts); setOptionsLoaded(true); }
      })
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 403) setAccessDenied(true);
        setOptions([]);
        setOptionsLoaded(true);
      });

    const loadSet = projectId && modelId
      ? getDrillThroughSet(projectId, modelId, measureId).then(set => {
          if (!cancelled) setDrillSet(set);
        }).catch(() => { if (!cancelled) setDrillSet(null); })
      : Promise.resolve();

    Promise.all([loadOptions, loadSet]).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [open, measureId, context, projectId, modelId]);

  const loadDrillThrough = useCallback(async (pathId: string, resetResults = true) => {
    if (!pathId) return;
    setLoading(true);
    try {
      const cursor = resetResults ? undefined : nextCursor;
      const res = await drillThrough(measureId, { ...context, hierarchy_id: pathId }, cursor);
      // Bug-8453 / R5 finding F2: an RLS deny-all returns zero detail rows.
      // An empty drill grid would read as "this cell has no detail", which is
      // a claim about the business rather than about the user's access.
      if (rowSecurityDeniedAll(res)) {
        setResult(null);
        setAllRows([]);
        setHasMore(false);
        setRowSecurityDenied(true);
        return;
      }
      setRowSecurityDenied(false);
      if (resetResults) {
        setAllRows(res.rows);
        setResult(res);
        setHasMore(res.page.has_more);
      } else {
        const newRows = [...allRows, ...res.rows];
        setHasMore(res.page.has_more);
        setAllRows(newRows);
        setResult(prev => prev ? { ...prev, rows: [...prev.rows, ...res.rows] } : res);
      }
      setNextCursor(res.page.next_cursor);
      setPage(0);
    } catch {
      setResult(null);
      setAllRows([]);
    } finally {
      setLoading(false);
    }
  }, [measureId, context, nextCursor, allRows]);

  const handlePathSelect = useCallback((id: string) => {
    setSelectedPath(id);
    setResult(null);
    setAllRows([]);
    setNextCursor(undefined);
    loadDrillThrough(id, true);
  }, [loadDrillThrough]);

  const loadMore = useCallback(async () => {
    if (!nextCursor || !selectedPath) return;
    setLoading(true);
    try {
      const res = await drillThrough(measureId, { ...context, hierarchy_id: selectedPath }, nextCursor);
      // R6 finding 5: this is the LOAD-MORE path, so rows are already on
      // screen. Leaving them there under a blanket denial notice showed a
      // partial extract next to a message implying nothing was visible --
      // two contradictory statements. Clear the grid so the notice is the
      // only claim being made, matching the initial-load branch above.
      if (rowSecurityDeniedAll(res)) {
        setResult(null);
        setAllRows([]);
        setHasMore(false);
        setNextCursor(undefined);
        setRowSecurityDenied(true);
        return;
      }
      const newRows = [...allRows, ...res.rows];
      setHasMore(res.page.has_more);
      setAllRows(newRows);
      setResult(prev => prev ? { ...prev, rows: [...prev.rows, ...res.rows] } : res);
      setNextCursor(res.page.next_cursor);
    } catch {
      // keep existing results
    } finally {
      setLoading(false);
    }
  }, [measureId, context, selectedPath, nextCursor, allRows]);

  const handleCopyTsv = useCallback(() => {
    if (!result) return;
    const hdrs = result.columns;
    const rws = allRows.map(r =>
      result.columns.map(c => String(r[c] ?? '')).join('\t')
    );
    const tsv = [hdrs.join('\t'), ...rws].join('\n');
    navigator.clipboard.writeText(tsv).catch(() => {});
  }, [result, allRows]);

  const breadcrumbs: BreadcrumbSegment[] = [
    {
      label: measureName,
      onClick: selectedPath ? () => {
        setSelectedPath('');
        setResult(null);
        setAllRows([]);
      } : undefined,
    },
  ];
  if (context.dimension) {
    breadcrumbs.push({ label: String(context.dimension) });
  }
  if (selectedPath) {
    const selectedOpt = options.find(o => o.hierarchy_id === selectedPath);
    if (selectedOpt) {
      breadcrumbs.push({
        label: selectedOpt.current_level_name || selectedOpt.hierarchy_name,
        onClick: () => loadDrillThrough(selectedPath, true),
      });
    }
  }

  const configuredColumns = drillSet?.detail_columns.map(c => c.name);
  const headers = configuredColumns && result
    ? configuredColumns.filter(c => result.columns.includes(c))
    : (result?.columns || []);
  const rows = allRows.map(r =>
    headers.map(c => String(r[c] ?? ''))
  ) as (string | number)[][];

  const pagedRows = rows.slice(page * DRILL_PAGE_SIZE, (page + 1) * DRILL_PAGE_SIZE);
  const totalPages = Math.ceil(rows.length / DRILL_PAGE_SIZE);

  if (!open) return null;

  return (
    <Box
      sx={{
        position: 'absolute', top: 0, right: 0, width: '100%', height: '100%',
        bgcolor: tokens.colorWhite, zIndex: 100,
        display: 'flex', flexDirection: 'column',
        borderLeft: `1px solid ${tokens.colorBorderLight}`,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', px: 1.5, py: 0.75, borderBottom: `1px solid ${tokens.colorBorderLight}` }}>
        <Typography sx={{ fontSize: 13, fontWeight: 700, flex: 1 }}>
          {strings.drill.title}
        </Typography>
        <IconButton size="small" onClick={onClose}>
          <Close sx={{ fontSize: 18 }} />
        </IconButton>
      </Box>

      <Box sx={{ px: 1.5, py: 0.5, borderBottom: `1px solid ${tokens.colorBorderLight}`, display: 'flex', alignItems: 'center', gap: 0.25, flexWrap: 'wrap' }}>
        {breadcrumbs.map((seg, i) => (
          <Box key={i} sx={{ display: 'flex', alignItems: 'center', gap: 0.25 }}>
            {i > 0 && <NavigateNext sx={{ fontSize: 14, color: tokens.colorTextSecondary }} />}
            {seg.onClick ? (
              <Typography
                sx={{ fontSize: 11, color: tokens.colorPrimary, cursor: 'pointer', '&:hover': { textDecoration: 'underline' } }}
                onClick={seg.onClick}
              >
                {seg.label}
              </Typography>
            ) : (
              <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary }}>
                {seg.label}
              </Typography>
            )}
          </Box>
        ))}
      </Box>

      {options.length > 0 && (
        <DrillPathPicker
          options={options}
          selectedPath={selectedPath}
          onSelect={handlePathSelect}
        />
      )}

      {loading && (
        <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <CircularProgress size={24} sx={{ color: tokens.colorPrimary }} />
        </Box>
      )}

      {result && !loading && (
        <Box sx={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          <Box sx={{ px: 1.5, py: 0.5, display: 'flex', alignItems: 'center', gap: 0.5, flexWrap: 'wrap' }}>
            <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, flex: 1 }}>
              {templates.drill.detailRowsLoaded(rows.length)}{hasMore ? strings.drill.moreAvailable : ''}
            </Typography>
            <Button
              size="small"
              variant="outlined"
              startIcon={<ContentCopy sx={{ fontSize: 14 }} />}
              onClick={handleCopyTsv}
              sx={{ fontSize: 10, minWidth: 'auto', textTransform: 'none' }}
            >
              {strings.drill.copyTsv}
            </Button>
            <Button
              size="small"
              variant="outlined"
              onClick={() => onInsertSheet(headers, rows)}
              sx={{ fontSize: 10, minWidth: 'auto', textTransform: 'none' }}
            >
              {strings.drill.insertSheet}
            </Button>
          </Box>

          {totalPages > 1 && (
            <Box sx={{ px: 1.5, py: 0.25, display: 'flex', alignItems: 'center', gap: 0.5 }}>
              <Button
                size="small"
                variant="text"
                disabled={page === 0}
                onClick={() => setPage(p => p - 1)}
                sx={{ fontSize: 10, minWidth: 'auto', textTransform: 'none' }}
              >
                {strings.drill.prev}
              </Button>
              <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary }}>
                {templates.drill.page(page + 1, totalPages)}
              </Typography>
              <Button
                size="small"
                variant="text"
                disabled={page >= totalPages - 1}
                onClick={() => setPage(p => p + 1)}
                sx={{ fontSize: 10, minWidth: 'auto', textTransform: 'none' }}
              >
                {strings.drill.next}
              </Button>
            </Box>
          )}

          <Box sx={{ flex: 1, overflow: 'auto', px: 0.5 }}>
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  {headers.map((h, i) => (
                    <TableCell key={i} sx={{ fontSize: 10, fontWeight: 600, bgcolor: tokens.colorSubtleFill, py: 0.5 }}>
                      {h}
                    </TableCell>
                  ))}
                </TableRow>
              </TableHead>
              <TableBody>
                {pagedRows.map((row, ri) => (
                  <TableRow key={ri}>
                    {row.map((cell, ci) => (
                      <TableCell key={ci} sx={{ fontSize: 11, py: 0.25 }}>
                        {cell}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Box>

          {hasMore && (
            <Box sx={{ p: 1, display: 'flex', justifyContent: 'center' }}>
              <Button
                size="small"
                variant="text"
                onClick={loadMore}
                disabled={loading}
                sx={{ fontSize: 11, textTransform: 'none', color: tokens.colorPrimary }}
              >
                {strings.drill.loadMore}
              </Button>
            </Box>
          )}
        </Box>
      )}

      {/* Bug-8453 / R5 finding F2: name the permissions restriction rather
          than showing an empty drill grid the user reads as "no detail". */}
      {rowSecurityDenied && !loading && (
        <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', p: 3 }}>
          <Typography sx={{ fontSize: 11, color: tokens.colorRed, textAlign: 'center' }}>
            {strings.drill.rowSecurityDenied}
          </Typography>
        </Box>
      )}

      {accessDenied && !loading && (
        <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', p: 3 }}>
          <Typography sx={{ fontSize: 11, color: tokens.colorRed, textAlign: 'center' }}>
            {strings.drill.accessDenied}
          </Typography>
        </Box>
      )}

      {!result && !loading && !accessDenied && optionsLoaded && options.length === 0 && (
        <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', p: 3 }}>
          <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, textAlign: 'center' }}>
            {strings.drill.noPaths}
          </Typography>
        </Box>
      )}
    </Box>
  );
}
