import { useMemo, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Collapse,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  IconButton,
  InputAdornment,
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
  TableRow,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import DeleteSweepIcon from "@mui/icons-material/DeleteSweepOutlined";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import PauseIcon from "@mui/icons-material/PauseOutlined";
import PlayArrowIcon from "@mui/icons-material/PlayArrowOutlined";
import RefreshIcon from "@mui/icons-material/RefreshOutlined";
import SearchIcon from "@mui/icons-material/Search";
import { systemLogsApi } from "../../api/client";
import type { SystemLog, SystemLogFilters, SystemLogPurgeResponse } from "../../api/types";
import { useT } from "../../i18n";
import { safeLocalGet } from "../../utils/safeLocalStorage";

type DraftFilters = {
  q: string;
  service: string;
  level: string;
  fromDate: string;
  toDate: string;
};

const EMPTY_FILTERS: DraftFilters = {
  q: "",
  service: "",
  level: "",
  fromDate: "",
  toDate: "",
};

type SeverityColor = "default" | "info" | "warning" | "error";

const SEVERITY_COLORS: Record<string, SeverityColor> = {
  DEBUG: "default",
  INFO: "info",
  WARN: "warning",
  WARNING: "warning",
  ERROR: "error",
  FATAL: "error",
  CRITICAL: "error",
};

const SEVERITY_LABELS: Record<string, string> = {
  DEBUG: "systemLogs.levelDebug",
  INFO: "systemLogs.levelInfo",
  WARN: "systemLogs.levelWarning",
  WARNING: "systemLogs.levelWarning",
  ERROR: "systemLogs.levelError",
  FATAL: "systemLogs.levelCritical",
  CRITICAL: "systemLogs.levelCritical",
};

function localInputToUtcIso(value: string): string | undefined {
  if (!value) return undefined;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? undefined : parsed.toISOString();
}

function severityKey(level: string): string {
  return level.trim().toUpperCase();
}

function severityColor(level: string): SeverityColor {
  return SEVERITY_COLORS[severityKey(level)] ?? "default";
}

function severityLabel(level: string, t: ReturnType<typeof useT>): string {
  const key = SEVERITY_LABELS[severityKey(level)];
  if (key) return t(key);
  return level || t("systemLogs.unknownLevel");
}

function displayTimestamp(timestamp: string): string {
  const parsed = new Date(timestamp);
  return Number.isNaN(parsed.getTime()) ? timestamp : parsed.toLocaleString();
}

function errorDetail(error: unknown): string | undefined {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" ? detail : undefined;
}

export default function SystemLogsPanel() {
  const t = useT();
  const isSystemAdmin =
    typeof window !== "undefined" &&
    safeLocalGet("user_role", "") === "system_admin";
  const [filters, setFilters] = useState<DraftFilters>(EMPTY_FILTERS);
  const [paused, setPaused] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [purgeOpen, setPurgeOpen] = useState(false);
  const [purgeResult, setPurgeResult] = useState<SystemLogPurgeResponse | null>(null);

  const settingsQuery = useQuery({
    queryKey: ["system-log-settings"],
    queryFn: systemLogsApi.settings,
    enabled: isSystemAdmin,
    retry: false,
  });

  const apiFilters = useMemo<SystemLogFilters>(
    () => ({
      q: filters.q.trim() || undefined,
      service: filters.service || undefined,
      level: filters.level || undefined,
      from_date: localInputToUtcIso(filters.fromDate),
      to_date: localInputToUtcIso(filters.toDate),
    }),
    [filters],
  );
  const pageSize = settingsQuery.data?.page_size;
  const pollSeconds = settingsQuery.data?.poll_seconds;
  const pollInterval =
    settingsQuery.data?.enabled === true &&
    !paused &&
    typeof pollSeconds === "number" &&
    pollSeconds > 0
      ? pollSeconds * 1000
      : false;

  const logsQuery = useInfiniteQuery({
    queryKey: ["system-logs", apiFilters, pageSize],
    queryFn: ({ pageParam }) =>
      systemLogsApi.list({
        ...apiFilters,
        cursor: pageParam ?? undefined,
        limit: pageSize,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: isSystemAdmin && settingsQuery.isSuccess,
    refetchInterval: pollInterval,
    retry: false,
  });

  const purgeMutation = useMutation({
    mutationFn: systemLogsApi.purge,
    onSuccess: (result) => {
      setPurgeResult(result);
      setPurgeOpen(false);
      void settingsQuery.refetch();
      void logsQuery.refetch();
    },
  });

  const items = useMemo(() => {
    const seen = new Set<string>();
    const flattened: SystemLog[] = [];
    for (const page of logsQuery.data?.pages ?? []) {
      for (const item of page.items) {
        if (seen.has(item.id)) continue;
        seen.add(item.id);
        flattened.push(item);
      }
    }
    return flattened;
  }, [logsQuery.data]);

  const updateFilter = (name: keyof DraftFilters, value: string) => {
    setFilters((current) => ({ ...current, [name]: value }));
    setExpandedId(null);
  };

  if (!isSystemAdmin) return null;

  const settings = settingsQuery.data;
  const isLoading = settingsQuery.isLoading || logsQuery.isLoading;
  const settingsError = errorDetail(settingsQuery.error);
  const logsError = errorDetail(logsQuery.error);
  const retentionDisabled =
    settings !== undefined && (settings.retention_days <= 0 || settings.cutoff === null);
  const canPurge = Boolean(settings?.enabled && !retentionDisabled);

  return (
    <Box sx={{ borderTop: 1, borderColor: "divider", bgcolor: "background.default" }}>
      <Box
        sx={{
          px: 2,
          py: 1.25,
          display: "flex",
          alignItems: "center",
          gap: 1,
          flexWrap: "wrap",
          bgcolor: "background.paper",
        }}
      >
        <Box sx={{ flex: 1, minWidth: 220 }}>
          <Typography variant="h6" sx={{ fontWeight: 700 }}>
            {t("systemLogs.title")}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            {t("systemLogs.rawDescription")}
          </Typography>
        </Box>
        <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: "nowrap" }}>
          {settings ? t("systemLogs.pollingStatus", { seconds: settings.poll_seconds }) : ""}
        </Typography>
        <Button
          size="small"
          variant={paused ? "outlined" : "contained"}
          startIcon={paused ? <PlayArrowIcon /> : <PauseIcon />}
          onClick={() => setPaused((current) => !current)}
          disabled={!settings?.enabled}
        >
          {paused ? t("systemLogs.resume") : t("systemLogs.pause")}
        </Button>
        <Tooltip title={t("systemLogs.refresh")}>
          <span>
            <IconButton
              size="small"
              onClick={() => void logsQuery.refetch()}
              disabled={!settings?.enabled || logsQuery.isFetching}
              aria-label={t("systemLogs.refresh")}
            >
              <RefreshIcon />
            </IconButton>
          </span>
        </Tooltip>
        <Button
          size="small"
          color="error"
          variant="outlined"
          startIcon={<DeleteSweepIcon />}
          onClick={() => {
            purgeMutation.reset();
            setPurgeResult(null);
            setPurgeOpen(true);
          }}
          disabled={!canPurge || purgeMutation.isPending}
        >
          {t("systemLogs.purgeExpired")}
        </Button>
      </Box>

      <Box sx={{ px: 2, py: 1, display: "flex", gap: 1, flexWrap: "wrap" }}>
        <TextField
          size="small"
          label={t("systemLogs.search")}
          value={filters.q}
          onChange={(event) => updateFilter("q", event.target.value)}
          sx={{ minWidth: 240, flex: "1 1 240px" }}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <SearchIcon sx={{ fontSize: 18, color: "text.secondary" }} />
              </InputAdornment>
            ),
          }}
        />
        <FormControl size="small" sx={{ minWidth: 170 }}>
          <InputLabel>{t("systemLogs.service")}</InputLabel>
          <Select
            SelectDisplayProps={{ "aria-label": t("systemLogs.service") }}
            value={filters.service}
            label={t("systemLogs.service")}
            onChange={(event) => updateFilter("service", event.target.value)}
          >
            <MenuItem value="">{t("systemLogs.allServices")}</MenuItem>
            {(settings?.services ?? []).map((service) => (
              <MenuItem key={service} value={service}>{service}</MenuItem>
            ))}
          </Select>
        </FormControl>
        <FormControl size="small" sx={{ minWidth: 150 }}>
          <InputLabel>{t("systemLogs.level")}</InputLabel>
          <Select
            SelectDisplayProps={{ "aria-label": t("systemLogs.level") }}
            value={filters.level}
            label={t("systemLogs.level")}
            onChange={(event) => updateFilter("level", event.target.value)}
          >
            <MenuItem value="">{t("systemLogs.allLevels")}</MenuItem>
            {(settings?.levels ?? []).map((level) => (
              <MenuItem key={level} value={level}>{severityLabel(level, t)}</MenuItem>
            ))}
          </Select>
        </FormControl>
        <TextField
          size="small"
          type="datetime-local"
          label={t("systemLogs.from")}
          value={filters.fromDate}
          onChange={(event) => updateFilter("fromDate", event.target.value)}
          InputLabelProps={{ shrink: true }}
          sx={{ minWidth: 205 }}
        />
        <TextField
          size="small"
          type="datetime-local"
          label={t("systemLogs.to")}
          value={filters.toDate}
          onChange={(event) => updateFilter("toDate", event.target.value)}
          InputLabelProps={{ shrink: true }}
          sx={{ minWidth: 205 }}
        />
      </Box>

      <Box sx={{ px: 2, pb: 2 }}>
        {settingsQuery.isLoading && (
          <Stack direction="row" spacing={1} alignItems="center" sx={{ py: 2 }}>
            <CircularProgress size={20} />
            <Typography variant="body2">{t("systemLogs.loading")}</Typography>
          </Stack>
        )}
        {settingsQuery.isError && (
          <Alert
            severity="error"
            action={
              <Button color="inherit" size="small" onClick={() => void settingsQuery.refetch()}>
                {t("systemLogs.retry")}
              </Button>
            }
          >
            {settingsError ?? t("systemLogs.loadFailed")}
          </Alert>
        )}
        {settings && !settings.enabled && (
          <Alert severity="info">{t("systemLogs.disabled")}</Alert>
        )}
        {retentionDisabled && (
          <Alert severity="info" sx={{ mb: 1 }}>
            {t("systemLogs.retentionDisabled")}
          </Alert>
        )}
        {purgeResult && (
          <Alert severity="success" sx={{ mb: 1 }}>
            {t("systemLogs.purgeSuccess", {
              count: purgeResult.deleted,
              cutoff: purgeResult.cutoff ?? t("systemLogs.unknownCutoff"),
            })}
          </Alert>
        )}
        {isLoading && (
          <Stack direction="row" spacing={1} alignItems="center" sx={{ py: 2 }}>
            <CircularProgress size={20} />
            <Typography variant="body2">{t("systemLogs.loading")}</Typography>
          </Stack>
        )}
        {logsQuery.isError && (
          <Alert
            severity="error"
            sx={{ mb: items.length ? 1 : 0 }}
            action={
              <Button color="inherit" size="small" onClick={() => void logsQuery.refetch()}>
                {t("systemLogs.retry")}
              </Button>
            }
          >
            {logsError ?? t("systemLogs.loadFailed")}
          </Alert>
        )}
        {!isLoading && !logsQuery.isError && items.length === 0 && (
          <Typography variant="body2" color="text.secondary" sx={{ py: 2 }}>
            {t("systemLogs.empty")}
          </Typography>
        )}
        {items.length > 0 && (
          <>
            <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: 480 }}>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    <TableCell>{t("systemLogs.timestamp")}</TableCell>
                    <TableCell>{t("systemLogs.service")}</TableCell>
                    <TableCell>{t("systemLogs.level")}</TableCell>
                    <TableCell>{t("systemLogs.logger")}</TableCell>
                    <TableCell>{t("systemLogs.instance")}</TableCell>
                    <TableCell>{t("systemLogs.message")}</TableCell>
                    <TableCell align="right">{t("systemLogs.details")}</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {items.map((item) => {
                    const expanded = item.id === expandedId;
                    return (
                      <SystemLogRow
                        key={item.id}
                        item={item}
                        expanded={expanded}
                        onToggle={() => setExpandedId(expanded ? null : item.id)}
                        t={t}
                      />
                    );
                  })}
                </TableBody>
              </Table>
            </TableContainer>
            <Stack direction="row" spacing={1} alignItems="center" justifyContent="flex-end" sx={{ pt: 1 }}>
              <Typography variant="caption" color="text.secondary" sx={{ mr: "auto" }}>
                {t("systemLogs.rowsShown", { count: items.length })}
              </Typography>
              <Button
                size="small"
                variant="outlined"
                disabled={!logsQuery.hasNextPage || logsQuery.isFetchingNextPage}
                onClick={() => void logsQuery.fetchNextPage()}
              >
                {logsQuery.isFetchingNextPage ? <CircularProgress size={16} /> : t("systemLogs.loadOlder")}
              </Button>
            </Stack>
          </>
        )}
      </Box>

      <Dialog
        open={purgeOpen}
        onClose={purgeMutation.isPending ? undefined : () => setPurgeOpen(false)}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle>{t("systemLogs.purgeTitle")}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" sx={{ mb: 1.5 }}>
            {retentionDisabled ? t("systemLogs.retentionDisabled") : t("systemLogs.purgeMessage")}
          </Typography>
          <Typography variant="body2">
            <strong>{t("systemLogs.cutoff")}</strong>{" "}
            <Box component="code" data-testid="system-logs-purge-cutoff" sx={{ fontFamily: "monospace" }}>
              {settings?.cutoff ?? t("systemLogs.unknownCutoff")}
            </Box>
          </Typography>
          {purgeMutation.isError && (
            <Alert severity="error" sx={{ mt: 2 }}>
              {errorDetail(purgeMutation.error) ?? t("systemLogs.purgeFailed")}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setPurgeOpen(false)} disabled={purgeMutation.isPending}>
            {t("systemLogs.cancel")}
          </Button>
          <Button
            color="error"
            variant="contained"
            onClick={() => purgeMutation.mutate()}
            disabled={!canPurge || purgeMutation.isPending}
          >
            {purgeMutation.isPending ? <CircularProgress size={16} color="inherit" /> : t("systemLogs.confirmPurge")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}

function SystemLogRow({
  item,
  expanded,
  onToggle,
  t,
}: {
  item: SystemLog;
  expanded: boolean;
  onToggle: () => void;
  t: ReturnType<typeof useT>;
}) {
  return (
    <>
      <TableRow
        hover
        onClick={onToggle}
        sx={{ cursor: "pointer", "& > td": { verticalAlign: "top" } }}
      >
        <TableCell sx={{ whiteSpace: "nowrap" }}>{displayTimestamp(item.timestamp)}</TableCell>
        <TableCell sx={{ whiteSpace: "nowrap" }}>{item.service}</TableCell>
        <TableCell>
          <Chip
            size="small"
            color={severityColor(item.level)}
            label={severityLabel(item.level, t)}
            title={item.level}
          />
        </TableCell>
        <TableCell sx={{ maxWidth: 160, overflowWrap: "anywhere" }}>{item.logger}</TableCell>
        <TableCell sx={{ maxWidth: 160, overflowWrap: "anywhere" }}>{item.instance}</TableCell>
        <TableCell sx={{ minWidth: 260, maxWidth: 520 }}>
          <Typography
            component="pre"
            variant="body2"
            sx={{ m: 0, whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontFamily: "inherit" }}
          >
            {item.message}
          </Typography>
        </TableCell>
        <TableCell align="right">
          <Button
            size="small"
            onClick={(event) => {
              event.stopPropagation();
              onToggle();
            }}
            endIcon={expanded ? <ExpandLessIcon /> : <ExpandMoreIcon />}
          >
            {expanded ? t("systemLogs.hideDetails") : t("systemLogs.details")}
          </Button>
        </TableCell>
      </TableRow>
      <TableRow>
        <TableCell colSpan={7} sx={{ p: 0, border: 0 }}>
          <Collapse in={expanded} timeout="auto" unmountOnExit>
            <Box sx={{ px: 2, py: 1.5, bgcolor: "grey.50" }}>
              <Stack spacing={0.5}>
                <Typography variant="caption"><strong>{t("systemLogs.id")}:</strong> {item.id}</Typography>
                <Typography variant="caption"><strong>{t("systemLogs.timestamp")}:</strong> {item.timestamp}</Typography>
                <Typography variant="caption"><strong>{t("systemLogs.logger")}:</strong> {item.logger}</Typography>
                <Typography variant="caption"><strong>{t("systemLogs.instance")}:</strong> {item.instance}</Typography>
                <Typography
                  component="pre"
                  variant="body2"
                  sx={{ m: 0, mt: 0.5, whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontFamily: "inherit" }}
                >
                  {item.message}
                </Typography>
              </Stack>
            </Box>
          </Collapse>
        </TableCell>
      </TableRow>
    </>
  );
}
