import { useMemo, useState } from "react";
import { useT } from "../../i18n";
import {
  Box,
  Button,
  Chip,
  CircularProgress,
  FormControl,
  InputAdornment,
  InputLabel,
  MenuItem,
  Select,
  Slider,
  Stack,
  Switch,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import RestartAltIcon from "@mui/icons-material/RestartAlt";
import type { SystemSettingItem } from "../../api/client";
import CronScheduleField from "./CronScheduleField";

type Props = {
  item: SystemSettingItem;
  saving: boolean;
  onSave: (value: unknown) => Promise<void>;
};

/**
 * Renders one setting row driven by its registry definition and
 * optional UI metadata (label, ui_group, ui_control, unit).
 *
 * The component chooses a control based on ``ui_control`` (fallback
 * derived from ``type``) and displays a human label with the raw
 * registry key hidden in an affordable secondary line. Description
 * is rendered inline so operators don't need to hover a tooltip.
 */
export default function SettingFieldRenderer({ item, saving, onSave }: Props) {
  const t = useT();
  const initial = useMemo(() => stringify(item.value), [item.value]);
  const [text, setText] = useState<string>(initial);
  const [bool, setBool] = useState<boolean>(Boolean(item.value));
  const [selectValue, setSelectValue] = useState<string>(
    item.value === null || item.value === undefined ? "" : String(item.value),
  );
  const [numericValue, setNumericValue] = useState<number>(
    typeof item.value === "number" ? item.value : Number(item.default ?? 0),
  );
  const [list, setList] = useState<string[]>(
    Array.isArray(item.value) ? item.value.map(String) : [],
  );
  const [error, setError] = useState<string | null>(null);

  const control = resolveControl(item);
  const friendlyLabel = item.label ?? prettifyKey(item.key);
  const description = (item.ui_help ?? item.description ?? "").trim();
  const readOnly = Boolean(item.env_var);

  const dirty = (() => {
    switch (control) {
      case "switch":
        return bool !== Boolean(item.value);
      case "select":
        return selectValue !== (item.value === null || item.value === undefined ? "" : String(item.value));
      case "select-multi":
        return !arraysEqual(list, Array.isArray(item.value) ? item.value.map(String) : []);
      case "slider":
        return numericValue !== (typeof item.value === "number" ? item.value : numericValue);
      case "hour-of-day":
        return selectValue !== (item.value === null || item.value === undefined ? "" : String(item.value));
      default:
        return text !== initial;
    }
  })();

  const ERROR_MAP: Record<string, string> = {
    "value is required": t("settings.valueRequired"),
    "must be an integer": t("settings.mustBeInteger"),
    "must be a number": t("settings.mustBeNumber"),
    "invalid JSON": t("settings.invalidJson"),
  };

  async function handleSave() {
    setError(null);
    try {
      const parsed = buildValue({ item, control, text, bool, selectValue, list, numericValue });
      await onSave(parsed);
    } catch (e) {
      const msg = (e as Error).message;
      setError(ERROR_MAP[msg] ?? msg);
    }
  }

  function handleReset() {
    setText(stringify(item.default));
    setBool(Boolean(item.default));
    setSelectValue(
      item.default === null || item.default === undefined ? "" : String(item.default),
    );
    setNumericValue(typeof item.default === "number" ? item.default : 0);
    setList(Array.isArray(item.default) ? item.default.map(String) : []);
    setError(null);
  }

  return (
    <Box
      sx={{
        py: 1.5,
        px: 0.5,
        borderBottom: 1,
        borderColor: "divider",
        display: "grid",
        gridTemplateColumns: "minmax(260px, 1fr) minmax(280px, 2fr) auto",
        columnGap: 2.5,
        rowGap: 0.5,
        alignItems: "flex-start",
      }}
    >
      <Stack spacing={0.5}>
        <Stack direction="row" spacing={0.75} alignItems="center" flexWrap="wrap">
          <Typography variant="body2" sx={{ fontWeight: 600 }}>
            {friendlyLabel}
          </Typography>
          {item.restart_required && (
            <Tooltip title={t("settings.restartRequiredTooltip")}>
              <Chip
                size="small"
                color="warning"
                variant="outlined"
                icon={<RestartAltIcon />}
                label={t("settings.restartRequired")}
                sx={{ height: 20, fontSize: 11 }}
              />
            </Tooltip>
          )}
          {readOnly && (
            <Chip
              size="small"
              variant="outlined"
              label={t("settings.readOnlyEnv")}
              sx={{ height: 20, fontSize: 11 }}
            />
          )}
        </Stack>
        {description && (
          <Typography variant="caption" color="text.secondary">
            {description}
          </Typography>
        )}
        <Typography variant="caption" color="text.disabled" sx={{ fontSize: 11 }}>
          {t("settings.defaultValue")} <code>{stringify(item.default) || t("settings.none")}</code>
          {item.unit ? ` · ${item.unit}` : ""}
        </Typography>
      </Stack>

      <Box>
        {renderControl({
          item,
          control,
          text,
          setText,
          bool,
          setBool,
          selectValue,
          setSelectValue,
          numericValue,
          setNumericValue,
          list,
          setList,
          saving,
          readOnly,
          error,
          t,
        })}
      </Box>

      <Stack direction="row" spacing={0.5}>
        <Button
          size="small"
          variant="outlined"
          onClick={handleReset}
          disabled={saving || readOnly}
        >
          {t("settings.reset")}
        </Button>
        <Button
          size="small"
          variant="contained"
          onClick={handleSave}
          disabled={!dirty || saving || readOnly}
        >
          {saving ? <CircularProgress size={14} /> : t("settings.save")}
        </Button>
      </Stack>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Control rendering
// ---------------------------------------------------------------------------

type ControlName =
  | "switch"
  | "select"
  | "select-multi"
  | "cron"
  | "slider"
  | "number"
  | "hour-of-day"
  | "dict"
  | "url"
  | "text";

const HOUR_OPTIONS = Array.from({ length: 24 }, (_, i) => ({
  value: i,
  label: `${String(i).padStart(2, "0")}:00 UTC`,
}));

function resolveControl(item: SystemSettingItem): ControlName {
  if (item.ui_control) {
    return item.ui_control as ControlName;
  }
  // No automatic inference for hour-of-day — must be declared via ui_control.
  if (item.type === "bool") return "switch";
  if (item.type === "cron") return "cron";
  if (item.type === "int" || item.type === "float") return "number";
  if (item.type === "dict") return "dict";
  if (item.type === "list[str]") return "select-multi";
  return "text";
}

function renderControl(args: {
  item: SystemSettingItem;
  control: ControlName;
  text: string;
  setText: (v: string) => void;
  bool: boolean;
  setBool: (v: boolean) => void;
  selectValue: string;
  setSelectValue: (v: string) => void;
  numericValue: number;
  setNumericValue: (v: number) => void;
  list: string[];
  setList: (v: string[]) => void;
  saving: boolean;
  readOnly: boolean;
  error: string | null;
  t: (key: string, vars?: Record<string, string>) => string;
}) {
  const {
    item, control, text, setText, bool, setBool, selectValue, setSelectValue,
    numericValue, setNumericValue, list, setList, saving, readOnly, error, t,
  } = args;
  const disabled = saving || readOnly;

  switch (control) {
    case "switch":
      return (
        <Stack direction="row" spacing={1} alignItems="center">
          <Switch
            checked={bool}
            onChange={(e) => setBool(e.target.checked)}
            disabled={disabled}
          />
          <Typography variant="body2" color="text.secondary">
            {bool ? t("settings.enabled") : t("settings.disabled")}
          </Typography>
        </Stack>
      );

    case "select": {
      const choices = (item.ui_choices ?? []) as string[];
      return (
        <FormControl size="small" fullWidth error={Boolean(error)}>
          <InputLabel>{t("settings.value")}</InputLabel>
          <Select
            label={t("settings.value")}
            value={selectValue}
            onChange={(e) => setSelectValue(String(e.target.value))}
            disabled={disabled}
          >
            {choices.map((c) => (
              <MenuItem key={c} value={c}>
                {c}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      );
    }

    case "select-multi": {
      const choices = (item.ui_choices ?? []) as string[];
      return (
        <FormControl size="small" fullWidth error={Boolean(error)}>
          <InputLabel>{t("settings.value")}</InputLabel>
          <Select
            multiple
            label={t("settings.value")}
            value={list}
            onChange={(e) => {
              const v = e.target.value;
              setList(typeof v === "string" ? v.split(",") : (v as string[]));
            }}
            disabled={disabled}
            renderValue={(selected) => (selected as string[]).join(", ") || t("settings.none")}
          >
            {choices.map((c) => (
              <MenuItem key={c} value={c}>
                {c}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      );
    }

    case "cron":
      return (
        <CronScheduleField
          value={text}
          onChange={setText}
          disabled={disabled}
          error={error}
        />
      );

    case "slider": {
      const val = clampRatio(numericValue);
      return (
        <Stack direction="row" spacing={2} alignItems="center">
          <Slider
            value={val}
            min={0}
            max={1}
            step={0.01}
            onChange={(_, v) => setNumericValue(Array.isArray(v) ? v[0] : v)}
            disabled={disabled}
            valueLabelDisplay="auto"
            sx={{ flex: 1 }}
          />
          <TextField
            size="small"
            type="number"
            value={val}
            onChange={(e) => setNumericValue(Number(e.target.value))}
            disabled={disabled}
            inputProps={{ step: 0.01, min: 0, max: 1 }}
            sx={{ width: 96 }}
          />
        </Stack>
      );
    }

    case "hour-of-day":
      return (
        <FormControl size="small" fullWidth error={Boolean(error)}>
          <Select
            value={selectValue}
            onChange={(e) => setSelectValue(String(e.target.value))}
            disabled={disabled}
          >
            {HOUR_OPTIONS.map((h) => (
              <MenuItem key={h.value} value={String(h.value)}>
                {h.label}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      );

    case "number":
      return (
        <TextField
          size="small"
          fullWidth
          type="number"
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={disabled}
          error={Boolean(error)}
          helperText={error || undefined}
          InputProps={
            item.unit
              ? { endAdornment: <InputAdornment position="end">{item.unit}</InputAdornment> }
              : undefined
          }
        />
      );

    case "dict":
      return (
        <TextField
          size="small"
          fullWidth
          multiline
          minRows={3}
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={disabled}
          error={Boolean(error)}
          helperText={error || t("settings.jsonObject")}
          sx={{ "& textarea": { fontFamily: "monospace", fontSize: 12 } }}
        />
      );

    case "url":
      return (
        <TextField
          size="small"
          fullWidth
          placeholder={t("settings.urlPlaceholder")}
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={disabled}
          error={Boolean(error)}
          helperText={error || undefined}
        />
      );

    default:
      return (
        <TextField
          size="small"
          fullWidth
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={disabled}
          error={Boolean(error)}
          helperText={error || undefined}
          InputProps={
            item.unit
              ? { endAdornment: <InputAdornment position="end">{item.unit}</InputAdornment> }
              : undefined
          }
        />
      );
  }
}

// ---------------------------------------------------------------------------
// Parsing / formatting
// ---------------------------------------------------------------------------

function buildValue(args: {
  item: SystemSettingItem;
  control: ControlName;
  text: string;
  bool: boolean;
  selectValue: string;
  list: string[];
  numericValue: number;
}): unknown {
  const { item, control, text, bool, selectValue, list, numericValue } = args;
  switch (control) {
    case "switch":
      return bool;
    case "select":
      if (!selectValue) throw new Error("value is required");
      return selectValue;
    case "select-multi":
      return list;
    case "slider":
      return clampRatio(numericValue);
    case "hour-of-day":
      return Number(selectValue);
    case "number":
      return parseScalar(item.type, text);
    case "dict":
      try {
        return JSON.parse(text);
      } catch {
        throw new Error("invalid JSON");
      }
    default:
      return parseScalar(item.type, text);
  }
}

function parseScalar(type: string, raw: string): unknown {
  const trimmed = raw.trim();
  if (trimmed === "") throw new Error("value is required");
  if (type === "int") {
    const n = Number(trimmed);
    if (!Number.isFinite(n) || !Number.isInteger(n)) {
      throw new Error("must be an integer");
    }
    return n;
  }
  if (type === "float") {
    const n = Number(trimmed);
    if (!Number.isFinite(n)) throw new Error("must be a number");
    return n;
  }
  if (type === "list[str]") {
    return trimmed
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  return raw;
}

function stringify(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

function prettifyKey(key: string): string {
  return key
    .split(".")
    .pop()!
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function arraysEqual(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const sa = [...a].sort();
  const sb = [...b].sort();
  return sa.every((v, i) => v === sb[i]);
}

function clampRatio(v: number): number {
  if (!Number.isFinite(v)) return 0;
  if (v < 0) return 0;
  if (v > 1) return 1;
  return v;
}
