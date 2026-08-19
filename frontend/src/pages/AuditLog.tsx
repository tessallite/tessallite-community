import { useCallback, useMemo, useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TablePagination,
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import DownloadIcon from "@mui/icons-material/FileDownloadOutlined";
import RefreshIcon from "@mui/icons-material/RefreshOutlined";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import { useQuery } from "@tanstack/react-query";
import { auditApi, type AuditEventFilters } from "../api/client";
import type { AuditEvent } from "../api/types";
import HelpIconButton from "../components/HelpIconButton";
import { useT } from "../i18n";

const SEVERITY_COLORS: Record<string, "error" | "warning" | "info" | "default"> = {
  critical: "error",
  warn: "warning",
  info: "info",
};

const PAGE_SIZES = [25, 50, 100];

// Bug-6315: the `datetime-local` filter inputs yield a naive local wall-clock
// string (e.g. "2026-07-01T14:30"). The audit timestamp column is stored in
// UTC (TIMESTAMPTZ) and rendered here in local time, so a raw local string
// sent to the API is interpreted as UTC — shifting the admin's chosen window
// by their timezone offset (both the list and the CSV export). Convert to a
// UTC ISO instant so the filter matches the timestamps the table displays.
export function localInputToUtcIso(value?: string): string | undefined {
  if (!value) return undefined;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

export default function AuditLog() {
  const t = useT();
  const [filters, setFilters] = useState<AuditEventFilters>({
    limit: 50,
    offset: 0,
  });
  const [expandedId, setExpandedId] = useState<string | null>(null);

  // Filters as sent to the API: local wall-clock date inputs converted to UTC
  // instants (Bug-6315). Display state (`filters`) keeps the raw local strings
  // so the datetime-local inputs stay bound correctly.
  const apiFilters = useMemo<AuditEventFilters>(
    () => ({
      ...filters,
      from_date: localInputToUtcIso(filters.from_date),
      to_date: localInputToUtcIso(filters.to_date),
    }),
    [filters],
  );

  const query = useQuery({
    queryKey: ["audit-events", apiFilters],
    queryFn: () => auditApi.list(apiFilters),
    refetchInterval: 30_000,
  });
  const actionsQuery = useQuery({
    queryKey: ["audit-event-actions"],
    queryFn: () => auditApi.listActions(),
  });

  const page = Math.floor((filters.offset ?? 0) / (filters.limit ?? 50));

  const handlePageChange = useCallback(
    (_: unknown, newPage: number) => {
      setFilters((f) => ({ ...f, offset: newPage * (f.limit ?? 50) }));
    },
    [],
  );

  const handleRowsPerPageChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setFilters((f) => ({ ...f, limit: parseInt(e.target.value, 10), offset: 0 }));
    },
    [],
  );

  const handleFilterChange = useCallback(
    (field: keyof AuditEventFilters, value: string) => {
      setFilters((f) => ({ ...f, [field]: value || undefined, offset: 0 }));
    },
    [],
  );

  const handleExport = useCallback(async () => {
    const blob = await auditApi.exportCsv(apiFilters);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "audit_events.csv";
    a.click();
    URL.revokeObjectURL(url);
  }, [apiFilters]);

  const actionOptions = useMemo(() => {
    const actions = new Set<string>(actionsQuery.data ?? []);
    if (filters.action) actions.add(filters.action);
    return Array.from(actions).sort();
  }, [actionsQuery.data, filters.action]);

  return (
    <Box sx={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      <Box
        sx={{
          px: 2,
          pt: 2,
          pb: 1,
          borderBottom: 1,
          borderColor: "divider",
          bgcolor: "background.paper",
          display: "flex",
          alignItems: "center",
          gap: 1,
        }}
      >
        <Typography variant="h6" sx={{ fontWeight: 700, flex: 1 }}>
          {t("auditLog.title")}
        </Typography>
        <HelpIconButton href="/help/admin/audit-log.html" />
        <Tooltip title={t("common.refresh")}>
          <IconButton size="small" onClick={() => query.refetch()}>
            <RefreshIcon />
          </IconButton>
        </Tooltip>
        <Button
          size="small"
          startIcon={<DownloadIcon />}
          onClick={handleExport}
          variant="outlined"
        >
          {t("auditLog.exportCsv")}
        </Button>
      </Box>

      <Box sx={{ px: 2, py: 1, display: "flex", gap: 1, flexWrap: "wrap" }}>
        <TextField
          size="small"
          label={t("auditLog.filterActorEmail")}
          value={filters.actor_email ?? ""}
          onChange={(e) => handleFilterChange("actor_email", e.target.value)}
          sx={{ width: 200 }}
        />
        <FormControl size="small" sx={{ width: 160 }}>
          <InputLabel>{t("auditLog.filterSeverity")}</InputLabel>
          <Select
            value={filters.severity ?? ""}
            label={t("auditLog.filterSeverity")}
            onChange={(e) => handleFilterChange("severity", e.target.value)}
          >
            <MenuItem value="">{t("auditLog.filterSeverityAll")}</MenuItem>
            <MenuItem value="critical">{t("auditLog.filterSeverityCritical")}</MenuItem>
            <MenuItem value="warn">{t("auditLog.filterSeverityWarn")}</MenuItem>
            <MenuItem value="info">{t("auditLog.filterSeverityInfo")}</MenuItem>
          </Select>
        </FormControl>
        <FormControl size="small" sx={{ width: 200 }}>
          <InputLabel>{t("auditLog.filterAction")}</InputLabel>
          <Select
            value={filters.action ?? ""}
            label={t("auditLog.filterAction")}
            onChange={(e) => handleFilterChange("action", e.target.value)}
          >
            <MenuItem value="">{t("auditLog.filterActionAll")}</MenuItem>
            {actionOptions.map((action) => (
              <MenuItem key={action} value={action}>
                {action}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
        <TextField
          size="small"
          label={t("auditLog.filterTargetType")}
          value={filters.target_type ?? ""}
          onChange={(e) => handleFilterChange("target_type", e.target.value)}
          sx={{ width: 160 }}
        />
        <TextField
          size="small"
          label={t("auditLog.filterFrom")}
          type="datetime-local"
          value={filters.from_date ?? ""}
          onChange={(e) => handleFilterChange("from_date", e.target.value)}
          InputLabelProps={{ shrink: true }}
          sx={{ width: 200 }}
        />
        <TextField
          size="small"
          label={t("auditLog.filterTo")}
          type="datetime-local"
          value={filters.to_date ?? ""}
          onChange={(e) => handleFilterChange("to_date", e.target.value)}
          InputLabelProps={{ shrink: true }}
          sx={{ width: 200 }}
        />
      </Box>

      <Box sx={{ flex: 1, overflow: "auto", px: 2, pb: 1 }}>
        {query.isLoading ? (
          <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
            <CircularProgress size={28} />
          </Box>
        ) : query.isError ? (
          // A failed request used to fall through to "No audit events found",
          // which tells a compliance reviewer the opposite of the truth: an
          // unreadable log reads as a clean one.
          <Box sx={{ py: 4, display: "flex", justifyContent: "center" }}>
            <Alert
              severity="error"
              sx={{ maxWidth: 480 }}
              action={
                <Button
                  color="inherit"
                  size="small"
                  onClick={() => query.refetch()}
                >
                  {t("auditLog.retry")}
                </Button>
              }
            >
              {t("auditLog.loadFailed")}
            </Alert>
          </Box>
        ) : !query.data?.items.length ? (
          <Box sx={{ py: 4, textAlign: "center" }}>
            <Typography color="text.secondary">
              {t("auditLog.noEvents")}
            </Typography>
          </Box>
        ) : (
          <TableContainer component={Paper} variant="outlined">
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell sx={{ width: 40 }} />
                  <TableCell>{t("auditLog.colTimestamp")}</TableCell>
                  <TableCell>{t("auditLog.colActor")}</TableCell>
                  <TableCell>{t("auditLog.colAction")}</TableCell>
                  <TableCell>{t("auditLog.colTarget")}</TableCell>
                  <TableCell>{t("auditLog.colSeverity")}</TableCell>
                  <TableCell>{t("auditLog.colIp")}</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {query.data.items.map((event) => (
                  <EventRow
                    key={event.id}
                    event={event}
                    expanded={expandedId === event.id}
                    onToggle={() =>
                      setExpandedId((prev) =>
                        prev === event.id ? null : event.id,
                      )
                    }
                  />
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}
      </Box>

      <TablePagination
        component="div"
        count={query.data?.total ?? 0}
        page={page}
        onPageChange={handlePageChange}
        rowsPerPage={filters.limit ?? 50}
        onRowsPerPageChange={handleRowsPerPageChange}
        rowsPerPageOptions={PAGE_SIZES}
        sx={{ borderTop: 1, borderColor: "divider" }}
      />
    </Box>
  );
}

function EventRow({
  event,
  expanded,
  onToggle,
}: {
  event: AuditEvent;
  expanded: boolean;
  onToggle: () => void;
}) {
  const t = useT();
  const ts = new Date(event.timestamp);
  const formatted = ts.toLocaleString();

  return (
    <>
      <TableRow hover sx={{ "& td": { borderBottom: expanded ? 0 : undefined } }}>
        <TableCell padding="checkbox">
          {event.detail && Object.keys(event.detail).length > 0 && (
            <IconButton size="small" onClick={onToggle}>
              {expanded ? <ExpandLessIcon fontSize="small" /> : <ExpandMoreIcon fontSize="small" />}
            </IconButton>
          )}
        </TableCell>
        <TableCell sx={{ whiteSpace: "nowrap" }}>{formatted}</TableCell>
        <TableCell>{event.actor_email ?? t("common.na")}</TableCell>
        <TableCell>
          <Typography variant="body2" sx={{ fontFamily: "monospace", fontSize: 12 }}>
            {event.action}
          </Typography>
        </TableCell>
        <TableCell>
          {event.target_type && (
            <Typography variant="body2" component="span" color="text.secondary">
              {event.target_type + t("common.targetTypeSeparator")}
            </Typography>
          )}
          {event.target_name ?? t("common.na")}
        </TableCell>
        <TableCell>
          <Chip
            label={event.severity}
            size="small"
            color={SEVERITY_COLORS[event.severity] ?? "default"}
            variant="outlined"
          />
        </TableCell>
        <TableCell>{event.ip_address ?? t("common.na")}</TableCell>
      </TableRow>
      {expanded && event.detail && (
        <TableRow>
          <TableCell colSpan={7} sx={{ py: 1, px: 4, bgcolor: "action.hover" }}>
            <Typography
              variant="body2"
              component="pre"
              sx={{ fontFamily: "monospace", fontSize: 12, m: 0, whiteSpace: "pre-wrap" }}
            >
              {JSON.stringify(event.detail, null, 2)}
            </Typography>
          </TableCell>
        </TableRow>
      )}
    </>
  );
}
