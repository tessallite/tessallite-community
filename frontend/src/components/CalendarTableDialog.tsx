/**
 * CalendarTableDialog — Phase RA-1+ multi-calendar manager.
 *
 * A source can carry zero or many calendar tables; each one is registered
 * as a CalendarTable row and surfaces in the model as a ModelTable alias.
 * Time-variant measures pick which alias they want as their calendar.
 *
 * The "Current" tab lists every calendar registered on the source.
 * Auto-create / Get script / Bind existing each provision an additional
 * calendar plus its companion alias.
 */
import { useState } from "react";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  IconButton,
  InputLabel,
  List,
  ListItem,
  ListItemText,
  MenuItem,
  Select,
  Stack,
  Tab,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { calendarApi } from "../api/client";
import { useT } from "../i18n";

type Flow = "view" | "auto-create" | "script" | "bind";
type CalendarColumnKey =
  | "date_column"
  | "year_column"
  | "half_column"
  | "quarter_column"
  | "month_column"
  | "week_column"
  | "day_column";

const CALENDAR_TYPE_VALUES = [
  "standard",
  "fiscal",
  "iso_week",
  "retail_445",
  "hijri",
  "thai_buddhist",
] as const;

const CALENDAR_TYPE_COLUMNS: Record<string, Record<string, string>> = {
  standard: {
    date_column: "date_key", year_column: "year_no", half_column: "half_no",
    quarter_column: "quarter_no", month_column: "month_no", week_column: "week_no", day_column: "day_no",
  },
  fiscal: {
    date_column: "date_key", year_column: "year_no", half_column: "half_no",
    quarter_column: "quarter_no", month_column: "month_no", week_column: "week_no", day_column: "day_no",
  },
  iso_week: {
    date_column: "date_key", year_column: "iso_year", week_column: "iso_week", day_column: "iso_day_of_week",
  },
  retail_445: {
    date_column: "date_key", year_column: "retail_year", quarter_column: "retail_quarter",
    month_column: "retail_period", week_column: "retail_week",
  },
  hijri: {
    date_column: "date_key", year_column: "hijri_year", month_column: "hijri_month", day_column: "hijri_day",
  },
  thai_buddhist: {
    date_column: "date_key", year_column: "thai_year", half_column: "half_no",
    quarter_column: "quarter_no", month_column: "month_no", week_column: "week_no", day_column: "day_no",
  },
};

const CALENDAR_COLUMN_FIELDS: Array<{ key: CalendarColumnKey; labelKey: string }> = [
  { key: "date_column", labelKey: "calendar.dateColumn" },
  { key: "year_column", labelKey: "calendar.yearColumn" },
  { key: "half_column", labelKey: "calendar.halfColumn" },
  { key: "quarter_column", labelKey: "calendar.quarterColumn" },
  { key: "month_column", labelKey: "calendar.monthColumn" },
  { key: "week_column", labelKey: "calendar.weekColumn" },
  { key: "day_column", labelKey: "calendar.dayColumn" },
];

const EMPTY_BIND_COLUMNS: Record<CalendarColumnKey, string> = {
  date_column: "",
  year_column: "",
  half_column: "",
  quarter_column: "",
  month_column: "",
  week_column: "",
  day_column: "",
};

function bindColumnPayload(columns: Record<CalendarColumnKey, string>) {
  const payload: Partial<Record<CalendarColumnKey, string>> = {};
  for (const field of CALENDAR_COLUMN_FIELDS) {
    const value = columns[field.key].trim();
    if (value) payload[field.key] = value;
  }
  return payload;
}

function extractError(err: unknown, t: (key: string) => string): { message: string; ddl?: string } {
  if (!err) return { message: t("calendarTable.operationFailed") };
  const anyErr = err as {
    response?: { data?: { detail?: string | { message?: string; ddl?: string } } };
    message?: string;
  };
  const detail = anyErr.response?.data?.detail;
  if (typeof detail === "string") return { message: detail };
  if (detail && typeof detail === "object") {
    return {
      message: detail.message || t("calendarTable.operationFailed"),
      ddl: detail.ddl,
    };
  }
  return { message: anyErr.message || t("calendarTable.operationFailed") };
}

interface Props {
  open: boolean;
  onClose: () => void;
  projectId: string;
  modelId: string;
  sourceId: string;
  dialect: string;
}

export default function CalendarTableDialog({
  open,
  onClose,
  projectId,
  modelId,
  sourceId,
  dialect,
}: Props) {
  const t = useT();
  const qc = useQueryClient();
  const [flow, setFlow] = useState<Flow>("view");
  const [tableName, setTableName] = useState("calendar");
  const [aliasName, setAliasName] = useState("");
  const [startDate, setStartDate] = useState("2010-01-01");
  const [endDate, setEndDate] = useState("2035-12-31");
  const [calendarType, setCalendarType] = useState("standard");
  const [fiscalStartMonth, setFiscalStartMonth] = useState(4);
  const [scriptOutput, setScriptOutput] = useState<string | null>(null);
  const [autoCreatedAliases, setAutoCreatedAliases] = useState<string[]>([]);
  const [bindColumns, setBindColumns] =
    useState<Record<CalendarColumnKey, string>>(EMPTY_BIND_COLUMNS);

  const list = useQuery({
    queryKey: ["calendars", projectId, modelId, sourceId],
    queryFn: () => calendarApi.list(projectId, modelId, sourceId),
    enabled: open,
  });

  // Bug-5920: fetch backend-computed calendar type availability instead of
  // a hardcoded frontend flag, so a type becomes selectable the moment its
  // optional dependency is installed server-side.
  const typesQuery = useQuery({
    queryKey: ["calendarTypes", projectId, modelId, sourceId],
    queryFn: () => calendarApi.types(projectId, modelId, sourceId),
    enabled: open,
    staleTime: 5 * 60 * 1000,
  });
  const unavailableTypes = new Set(
    (typesQuery.data ?? [])
      .filter((t) => !t.available)
      .map((t) => t.calendar_type),
  );

  const invalidate = () => {
    qc.removeQueries({ queryKey: ["calendars", projectId, modelId, sourceId] });
    qc.invalidateQueries({ queryKey: ["sources", projectId, modelId] });
    qc.invalidateQueries({ queryKey: ["modelTables", projectId, modelId, sourceId] });
    qc.invalidateQueries({ queryKey: ["allModelTables", projectId, modelId] });
    // Calendar provisioning also creates a join (alias -> fact). The ERD canvas
    // reads edges from this query, so refresh it or the new link stays hidden
    // until the model is closed and reopened.
    qc.invalidateQueries({ queryKey: ["joins", projectId, modelId] });
  };

  const aliasField = aliasName.trim() || undefined;
  const effectiveFys = calendarType === "fiscal" ? fiscalStartMonth : 1;

  const scriptMut = useMutation({
    mutationFn: () =>
      calendarApi.script(projectId, modelId, sourceId, {
        dialect,
        table_name: tableName,
        start_date: startDate,
        end_date: endDate,
        calendar_type: calendarType,
        fiscal_year_start_month: effectiveFys,
      }),
    onSuccess: (r) => setScriptOutput(r.ddl),
  });

  const autoMut = useMutation({
    mutationFn: () =>
      calendarApi.autoCreate(projectId, modelId, sourceId, {
        table_name: tableName,
        start_date: startDate,
        end_date: endDate,
        alias: aliasField,
        fiscal_year_start_month: effectiveFys,
        calendar_type: calendarType,
      }),
    onSuccess: (data) => {
      invalidate();
      setAutoCreatedAliases(data.auto_created_aliases ?? []);
      setFlow("view");
    },
    onError: (error) => {
      const extracted = extractError(error, t);
      if (extracted.ddl) {
        setScriptOutput(extracted.ddl);
        setFlow("script");
      }
    },
  });

  const columnDefaults = CALENDAR_TYPE_COLUMNS[calendarType] ?? CALENDAR_TYPE_COLUMNS.standard;
  const hasRequiredBindColumn =
    Boolean(bindColumns.date_column.trim()) || Boolean(bindColumns.year_column.trim());

  const bindMut = useMutation({
    mutationFn: () =>
      calendarApi.bind(projectId, modelId, sourceId, {
        table_name: tableName,
        ...bindColumnPayload(bindColumns),
        alias: aliasField,
        fiscal_year_start_month: effectiveFys,
        calendar_type: calendarType,
      }),
    onSuccess: (data) => {
      invalidate();
      setAutoCreatedAliases(data.auto_created_aliases ?? []);
      setFlow("view");
    },
  });

  const deleteMut = useMutation({
    mutationFn: (calendarId: string) =>
      calendarApi.delete(projectId, modelId, sourceId, calendarId),
    onSuccess: invalidate,
  });

  function switchTab(next: Flow) {
    setFlow(next);
    autoMut.reset();
    scriptMut.reset();
    bindMut.reset();
    setScriptOutput(null);
    setAutoCreatedAliases([]);
    setBindColumns(EMPTY_BIND_COLUMNS);
  }

  const autoCreateError = autoMut.isError ? extractError(autoMut.error, t) : null;

  const CALENDAR_TYPE_OPTIONS = CALENDAR_TYPE_VALUES.map((v) => ({
    value: v,
    label: t(`calendar.${v === "iso_week" ? "isoWeek" : v === "retail_445" ? "retail445" : v === "thai_buddhist" ? "thaiBuddhist" : v}`),
    // Bug-5920: availability is now backend-computed (see typesQuery
    // above) instead of a hardcoded per-type flag.
    disabled: unavailableTypes.has(v),
  }));

  const FISCAL_MONTHS: Array<[number, string]> = [
    [2, t("calendar.months.february")],
    [3, t("calendar.months.march")],
    [4, t("calendar.months.april")],
    [5, t("calendar.months.may")],
    [6, t("calendar.months.june")],
    [7, t("calendar.months.july")],
    [8, t("calendar.months.august")],
    [9, t("calendar.months.september")],
    [10, t("calendar.months.october")],
    [11, t("calendar.months.november")],
    [12, t("calendar.months.december")],
  ];

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{t("calendar.dialogTitle")}</DialogTitle>
      <DialogContent>
        <Tabs
          value={flow}
          onChange={(_, v) => switchTab(v as Flow)}
          variant="fullWidth"
          sx={{ mb: 2 }}
        >
          <Tab label={t("calendar.tabCurrent")} value="view" />
          <Tab label={t("calendar.tabAutoCreate")} value="auto-create" />
          <Tab label={t("calendar.tabGetScript")} value="script" />
          <Tab label={t("calendar.tabBindExisting")} value="bind" />
        </Tabs>

        {/* -------- Current -------- */}
        {flow === "view" && (
          <Box>
            {autoCreatedAliases.length > 0 && (
              <Alert severity="info" sx={{ mb: 2 }} onClose={() => setAutoCreatedAliases([])}>
                {autoCreatedAliases.length === 1
                  ? t("calendar.autoCreatedSingle", { alias: autoCreatedAliases[0] })
                  : t("calendar.autoCreatedMultiple", {
                      count: String(autoCreatedAliases.length),
                      aliases: autoCreatedAliases.join(", "),
                    })}
                {" "}{t("calendar.autoCreatedSuffix")}
              </Alert>
            )}
            {list.isLoading && <CircularProgress size={20} />}
            {list.data && list.data.length > 0 ? (
              <List dense disablePadding>
                {list.data.map((c) => (
                  <ListItem
                    key={c.id}
                    disableGutters
                    secondaryAction={
                      <Tooltip title={t("calendar.deleteTooltip")}>
                        <span>
                          <IconButton
                            size="small"
                            onClick={() => deleteMut.mutate(c.id)}
                            disabled={deleteMut.isPending}
                          >
                            <DeleteOutlineIcon fontSize="small" />
                          </IconButton>
                        </span>
                      </Tooltip>
                    }
                  >
                    <ListItemText
                      primary={
                        <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                          <Typography variant="body2" component="span">
                            <strong>{c.table_name}</strong>
                          </Typography>
                          <Chip size="small" label={c.dialect} />
                          {c.calendar_type && c.calendar_type !== "standard" && (
                            <Chip size="small" label={c.calendar_type} color="primary" variant="outlined" />
                          )}
                          {c.autocreated && (
                            <Chip size="small" label={t("calendar.autoLabel")} color="info" variant="outlined" />
                          )}
                        </Box>
                      }
                      secondary={
                        <Typography variant="caption" color="text.secondary">
                          date={c.date_column ?? t("common.na")}, year={c.year_column ?? t("common.na")}, quarter=
                          {c.quarter_column ?? t("common.na")}, month={c.month_column ?? t("common.na")}
                        </Typography>
                      }
                    />
                  </ListItem>
                ))}
              </List>
            ) : (
              <Typography variant="body2" color="text.secondary">
                {t("calendar.noCalendarsDetail")}
              </Typography>
            )}
            {deleteMut.isError && (
              <Alert severity="error" sx={{ mt: 1 }}>
                {extractError(deleteMut.error, t).message}
              </Alert>
            )}
          </Box>
        )}

        {/* -------- Shared form fields -------- */}
        {(flow === "auto-create" || flow === "script" || flow === "bind") && (
          <Stack spacing={1.5}>
            <TextField
              label={t("calendar.physicalTableName")}
              size="small"
              fullWidth
              value={tableName}
              onChange={(e) => setTableName(e.target.value)}
              helperText={t("calendar.physicalTableHelp")}
            />
            {flow !== "script" && (
              <TextField
                label={t("calendar.aliasOptional")}
                size="small"
                fullWidth
                value={aliasName}
                onChange={(e) => setAliasName(e.target.value)}
                helperText={t("calendar.aliasHelp")}
              />
            )}
            {flow !== "bind" && (
              <Box sx={{ display: "flex", gap: 1 }}>
                <TextField
                  label={t("calendar.startDate")}
                  type="date"
                  size="small"
                  value={startDate}
                  onChange={(e) => setStartDate(e.target.value)}
                  sx={{ flex: 1 }}
                  InputLabelProps={{ shrink: true }}
                />
                <TextField
                  label={t("calendar.endDate")}
                  type="date"
                  size="small"
                  value={endDate}
                  onChange={(e) => setEndDate(e.target.value)}
                  sx={{ flex: 1 }}
                  InputLabelProps={{ shrink: true }}
                />
              </Box>
            )}
            {flow === "bind" && (
              <Typography variant="caption" color="text.secondary">
                {t("calendar.bindColumnHint")}
              </Typography>
            )}
            <FormControl size="small" fullWidth>
              <InputLabel id="calendar-type-label">{t("calendar.calendarType")}</InputLabel>
              <Select
                labelId="calendar-type-label"
                id="calendar-type-select"
                value={calendarType}
                label={t("calendar.calendarType")}
                onChange={(e) => setCalendarType(e.target.value)}
              >
                {CALENDAR_TYPE_OPTIONS.map((opt) => (
                  <MenuItem key={opt.value} value={opt.value} disabled={opt.disabled}>
                    {opt.disabled
                      ? `${opt.label} — ${t("calendar.hijriUnavailable")}`
                      : opt.label}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            {calendarType === "fiscal" && (
              <FormControl size="small" sx={{ minWidth: 180 }}>
                <InputLabel id="calendar-fiscal-month-label">{t("calendar.fiscalStartMonth")}</InputLabel>
                <Select
                  labelId="calendar-fiscal-month-label"
                  id="calendar-fiscal-month-select"
                  value={fiscalStartMonth}
                  label={t("calendar.fiscalStartMonth")}
                  onChange={(e) => setFiscalStartMonth(Number(e.target.value))}
                >
                  {FISCAL_MONTHS.map(([num, label]) => (
                    <MenuItem key={num} value={num}>{label}</MenuItem>
                  ))}
                </Select>
              </FormControl>
            )}
            {flow === "bind" && (
              <Box
                sx={{
                  display: "grid",
                  gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr" },
                  gap: 1,
                }}
              >
                {CALENDAR_COLUMN_FIELDS.map((field) => (
                  <TextField
                    key={field.key}
                    label={t(field.labelKey)}
                    size="small"
                    value={bindColumns[field.key]}
                    placeholder={columnDefaults[field.key] ?? ""}
                    helperText={field.key === "date_column" ? t("calendar.dateOrYearRequired") : " "}
                    onChange={(e) =>
                      setBindColumns((current) => ({
                        ...current,
                        [field.key]: e.target.value,
                      }))
                    }
                  />
                ))}
              </Box>
            )}
          </Stack>
        )}

        {/* -------- Auto-create: non-DDL errors (DDL errors redirect to Script tab) -------- */}
        {flow === "auto-create" && autoCreateError && !autoCreateError.ddl && (
          <Box sx={{ mt: 1.5 }}>
            <Alert severity="warning">
              {autoCreateError.message}
            </Alert>
          </Box>
        )}

        {/* -------- Script output -------- */}
        {flow === "script" && (
          <Box sx={{ mt: 2 }}>
            {scriptMut.isPending && (
              <Box sx={{ display: "flex", justifyContent: "center", py: 2 }}>
                <CircularProgress size={20} />
              </Box>
            )}
            {scriptMut.isError && (
              <Alert severity="error" sx={{ mb: 1 }}>
                {extractError(scriptMut.error, t).message}
              </Alert>
            )}
            {scriptOutput ? (
              <>
                <Typography variant="caption" color="text.secondary">
                  {t("calendar.scriptHint")}
                </Typography>
                <Box
                  component="pre"
                  sx={{
                    p: 1,
                    mt: 0.5,
                    bgcolor: "#f5f5f5",
                    fontSize: "0.75rem",
                    fontFamily: "monospace",
                    whiteSpace: "pre-wrap",
                    maxHeight: 240,
                    overflow: "auto",
                  }}
                >
                  {scriptOutput}
                </Box>
              </>
            ) : !scriptMut.isPending && !scriptMut.isError && (
              <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                {t("calendar.showDdlHint")}
              </Typography>
            )}
          </Box>
        )}

        {/* -------- Bind error -------- */}
        {flow === "bind" && bindMut.isError && (
          <Alert severity="error" sx={{ mt: 1 }}>
            {extractError(bindMut.error, t).message}
          </Alert>
        )}
      </DialogContent>

      {/* -------- Action buttons -------- */}
      <DialogActions>
        <Button onClick={onClose}>{t("calendar.close")}</Button>
        {flow === "auto-create" && (
          <Button
            variant="contained"
            onClick={() => autoMut.mutate()}
            disabled={autoMut.isPending || !tableName.trim()}
          >
            {autoMut.isPending ? <CircularProgress size={16} /> : t("calendar.generate")}
          </Button>
        )}
        {flow === "script" && !scriptOutput && (
          <Button
            variant="contained"
            onClick={() => scriptMut.mutate()}
            disabled={scriptMut.isPending || !tableName.trim()}
          >
            {scriptMut.isPending ? <CircularProgress size={16} /> : t("calendar.showDdl")}
          </Button>
        )}
        {flow === "script" && scriptOutput && (
          <Button
            size="small"
            onClick={() => scriptMut.mutate()}
            disabled={scriptMut.isPending}
          >
            {scriptMut.isPending ? <CircularProgress size={16} /> : t("calendar.refresh")}
          </Button>
        )}
        {flow === "bind" && (
          <Button
            variant="contained"
            onClick={() => bindMut.mutate()}
            disabled={bindMut.isPending || !tableName.trim() || !hasRequiredBindColumn}
          >
            {bindMut.isPending ? <CircularProgress size={16} /> : t("calendar.bind")}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  );
}
