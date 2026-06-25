import { useMemo, useState } from "react";
import { useT } from "../../i18n";
import {
  Box,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from "@mui/material";

type Props = {
  value: string;
  onChange: (next: string) => void;
  disabled?: boolean;
  error?: string | null;
};

type ScheduleMode = "daily" | "hourly" | "weekly" | "custom";

const WEEKDAYS = [
  { id: 1, label: "cron.weekday.mon" },
  { id: 2, label: "cron.weekday.tue" },
  { id: 3, label: "cron.weekday.wed" },
  { id: 4, label: "cron.weekday.thu" },
  { id: 5, label: "cron.weekday.fri" },
  { id: 6, label: "cron.weekday.sat" },
  { id: 0, label: "cron.weekday.sun" },
];

/**
 * Friendly schedule picker that writes a 5-field cron string behind
 * the scenes. End users pick "Every day at 02:00" instead of typing
 * "0 2 * * *". An "Advanced" toggle exposes the raw field for the
 * rare expression the picker cannot express.
 */
export default function CronScheduleField({
  value,
  onChange,
  disabled,
  error,
}: Props) {
  const t = useT();
  const parsed = useMemo(() => parseCron(value), [value]);
  const [mode, setMode] = useState<ScheduleMode>(parsed.mode);

  function emit(next: { mode: ScheduleMode; hour: number; minute: number; weekday: number; raw: string }) {
    const cron = composeCron(next);
    onChange(cron);
  }

  const summary = summarise(value, t);

  return (
    <Stack spacing={1}>
      <ToggleButtonGroup
        size="small"
        value={mode}
        exclusive
        onChange={(_, next) => {
          if (!next) return;
          setMode(next);
          emit({ ...parsed, mode: next });
        }}
        disabled={disabled}
      >
        <ToggleButton value="daily">{t("cron.daily")}</ToggleButton>
        <ToggleButton value="hourly">{t("cron.hourly")}</ToggleButton>
        <ToggleButton value="weekly">{t("cron.weekly")}</ToggleButton>
        <ToggleButton value="custom">{t("cron.advanced")}</ToggleButton>
      </ToggleButtonGroup>

      {mode === "daily" && (
        <TimeOfDayPicker
          hour={parsed.hour}
          minute={parsed.minute}
          disabled={disabled}
          onChange={(h, m) => emit({ ...parsed, mode, hour: h, minute: m })}
        />
      )}

      {mode === "hourly" && (
        <FormControl size="small" sx={{ width: 180 }}>
          <InputLabel>{t("cron.minuteOfHour")}</InputLabel>
          <Select
            label={t("cron.minuteOfHour")}
            value={parsed.minute}
            onChange={(e) =>
              emit({ ...parsed, mode, minute: Number(e.target.value) })
            }
            disabled={disabled}
          >
            {Array.from({ length: 60 }, (_, i) => (
              <MenuItem key={i} value={i}>
                {String(i).padStart(2, "0")}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      )}

      {mode === "weekly" && (
        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap">
          <FormControl size="small" sx={{ minWidth: 120 }}>
            <InputLabel>{t("cron.day")}</InputLabel>
            <Select
              label={t("cron.day")}
              value={parsed.weekday}
              onChange={(e) =>
                emit({ ...parsed, mode, weekday: Number(e.target.value) })
              }
              disabled={disabled}
            >
              {WEEKDAYS.map((d) => (
                <MenuItem key={d.id} value={d.id}>
                  {t(d.label)}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <TimeOfDayPicker
            hour={parsed.hour}
            minute={parsed.minute}
            disabled={disabled}
            onChange={(h, m) => emit({ ...parsed, mode, hour: h, minute: m })}
          />
        </Stack>
      )}

      {mode === "custom" && (
        <TextField
          size="small"
          fullWidth
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          error={Boolean(error)}
          helperText={error || t("cron.fiveFieldHelp")}
        />
      )}

      <Typography variant="caption" color="text.secondary">
        {summary}
      </Typography>
    </Stack>
  );
}

function TimeOfDayPicker({
  hour,
  minute,
  disabled,
  onChange,
}: {
  hour: number;
  minute: number;
  disabled?: boolean;
  onChange: (hour: number, minute: number) => void;
}) {
  const t = useT();
  return (
    <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
      <FormControl size="small" sx={{ width: 96 }}>
        <InputLabel>{t("cron.hour")}</InputLabel>
        <Select
          label={t("cron.hour")}
          value={hour}
          onChange={(e) => onChange(Number(e.target.value), minute)}
          disabled={disabled}
        >
          {Array.from({ length: 24 }, (_, i) => (
            <MenuItem key={i} value={i}>
              {String(i).padStart(2, "0")}
            </MenuItem>
          ))}
        </Select>
      </FormControl>
      <Typography>:</Typography>
      <FormControl size="small" sx={{ width: 96 }}>
        <InputLabel>{t("cron.minute")}</InputLabel>
        <Select
          label={t("cron.minute")}
          value={minute}
          onChange={(e) => onChange(hour, Number(e.target.value))}
          disabled={disabled}
        >
          {Array.from({ length: 60 }, (_, i) => (
            <MenuItem key={i} value={i}>
              {String(i).padStart(2, "0")}
            </MenuItem>
          ))}
        </Select>
      </FormControl>
    </Box>
  );
}

function parseCron(value: string): {
  mode: ScheduleMode;
  hour: number;
  minute: number;
  weekday: number;
  raw: string;
} {
  const parts = (value || "").trim().split(/\s+/);
  const def = { mode: "custom" as ScheduleMode, hour: 2, minute: 0, weekday: 1, raw: value };
  if (parts.length !== 5) return def;
  const [minute, hour, dom, month, dow] = parts;
  const m = Number(minute);
  const h = Number(hour);
  if (Number.isFinite(m) && Number.isFinite(h) && m >= 0 && m <= 59 && h >= 0 && h <= 23) {
    if (dom === "*" && month === "*" && dow === "*") {
      return { mode: "daily", hour: h, minute: m, weekday: 1, raw: value };
    }
    if (dom === "*" && month === "*" && /^\d+$/.test(dow)) {
      return {
        mode: "weekly",
        hour: h,
        minute: m,
        weekday: Number(dow),
        raw: value,
      };
    }
    if (hour === "*" && dom === "*" && month === "*" && dow === "*") {
      return { mode: "hourly", hour: 0, minute: m, weekday: 1, raw: value };
    }
  }
  return def;
}

function composeCron(x: {
  mode: ScheduleMode;
  hour: number;
  minute: number;
  weekday: number;
  raw: string;
}): string {
  if (x.mode === "daily") return `${x.minute} ${x.hour} * * *`;
  if (x.mode === "hourly") return `${x.minute} * * * *`;
  if (x.mode === "weekly") return `${x.minute} ${x.hour} * * ${x.weekday}`;
  return x.raw;
}

function summarise(cron: string, t: (key: string, vars?: Record<string, string>) => string): string {
  const p = parseCron(cron);
  const hh = String(p.hour).padStart(2, "0");
  const mm = String(p.minute).padStart(2, "0");
  if (p.mode === "daily") return t("cron.summaryDaily", { time: `${hh}:${mm}` });
  if (p.mode === "hourly") return t("cron.summaryHourly", { minute: mm });
  if (p.mode === "weekly") {
    const dayKey = WEEKDAYS.find((w) => w.id === p.weekday)?.label ?? "?";
    const d = dayKey !== "?" ? t(dayKey) : "?";
    return t("cron.summaryWeekly", { day: d, time: `${hh}:${mm}` });
  }
  if (!cron) return t("cron.summaryCustomUnset");
  return t("cron.summaryCustom", { cron });
}
