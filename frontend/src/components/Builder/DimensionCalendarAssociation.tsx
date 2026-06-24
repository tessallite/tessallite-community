/**
 * DimensionCalendarAssociation — shown inside the Add/Edit Dimension dialog
 * when the selected attribute has a date/timestamp type. Lets the user
 * pick a calendar from the model, optionally create a dedicated alias, and
 * trigger hierarchy pre-creation based on the calendar type.
 */
import { useEffect, useState } from "react";
import { useT } from "../../i18n";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  CircularProgress,
  FormControl,
  FormControlLabel,
  InputLabel,
  MenuItem,
  Select,
  TextField,
  Typography,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { calendarApi } from "../../api/client";
import type { CalendarCoverageResponse, CalendarTable } from "../../api/types";

const HIERARCHY_PRESETS_RAW: Record<string, string[]> = {
  standard: ["Year", "Half", "Quarter", "Month", "Week", "Day"],
  fiscal: ["Fiscal Year", "Fiscal Half", "Fiscal Quarter", "Fiscal Month", "Day"],
  iso_week: ["ISO Year", "ISO Week", "ISO Day"],
  retail_445: ["Retail Year", "Retail Quarter", "Retail Period", "Retail Week"],
  hijri: ["Hijri Year", "Hijri Month", "Hijri Day"],
  thai_buddhist: ["Thai Year", "Quarter", "Month", "Day"],
};

interface Props {
  projectId: string;
  modelId: string;
  sourceId: string;
  visible: boolean;
  // F-016-23: the fact table + date column being associated, used for the
  // optional coverage check against the chosen calendar's date range.
  factTable?: string;
  factDateColumn?: string;
  /** Bug-5245/5297: seed the picker with the existing calendar binding so
   *  re-opening an edit dialog shows the current association, not blank. */
  initialCalendarId?: string | null;
  onCalendarSelect: (calendarId: string | null, calendarType: string | null) => void;
  onHierarchyLevels: (levels: string[]) => void;
}

export default function DimensionCalendarAssociation({
  projectId,
  modelId,
  sourceId,
  visible,
  factTable,
  factDateColumn,
  initialCalendarId,
  onCalendarSelect,
  onHierarchyLevels,
}: Props) {
  const t = useT();
  const [selectedCalendarId, setSelectedCalendarId] = useState("");
  const [createAlias, setCreateAlias] = useState(false);
  const [aliasName, setAliasName] = useState("");
  const [showHierarchy, setShowHierarchy] = useState(false);
  const [hierarchyLevels, setHierarchyLevels] = useState<string[]>([]);
  const [coverage, setCoverage] = useState<CalendarCoverageResponse | null>(null);
  const [coverageLoading, setCoverageLoading] = useState(false);
  const [coverageError, setCoverageError] = useState<string | null>(null);

  const calendars = useQuery({
    queryKey: ["calendars", projectId, modelId, sourceId],
    queryFn: () => calendarApi.list(projectId, modelId, sourceId),
    enabled: visible && !!sourceId,
  });

  // Bug-5245/5297: seed from initialCalendarId so the edit dialog shows the
  // existing association instead of a blank picker.
  useEffect(() => {
    if (initialCalendarId && !selectedCalendarId) {
      setSelectedCalendarId(initialCalendarId);
    }
    // Run only when the prop changes (dialog open/close resets it).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialCalendarId]);

  async function handleCheckCoverage() {
    if (!selectedCalendarId || !factTable || !factDateColumn) return;
    setCoverageLoading(true);
    setCoverageError(null);
    setCoverage(null);
    try {
      const result = await calendarApi.coverage(
        projectId, modelId, sourceId, selectedCalendarId, factTable, factDateColumn,
      );
      setCoverage(result);
    } catch {
      setCoverageError(t("dimCalendar.coverageFailed"));
    } finally {
      setCoverageLoading(false);
    }
  }

  if (!visible) return null;

  const calendarList: CalendarTable[] = calendars.data ?? [];
  const selectedCalendar = calendarList.find((c) => c.id === selectedCalendarId);

  function handleCalendarChange(calendarId: string) {
    setSelectedCalendarId(calendarId);
    setCoverage(null);
    setCoverageError(null);
    const cal = calendarList.find((c) => c.id === calendarId);
    const calType = cal?.calendar_type ?? "standard";
    onCalendarSelect(calendarId || null, calType || null);

    const preset = HIERARCHY_PRESETS_RAW[calType] ?? HIERARCHY_PRESETS_RAW.standard;
    setHierarchyLevels(preset);
    onHierarchyLevels(preset);
  }

  function handleLevelToggle(level: string) {
    const updated = hierarchyLevels.includes(level)
      ? hierarchyLevels.filter((l) => l !== level)
      : [...hierarchyLevels, level];
    setHierarchyLevels(updated);
    onHierarchyLevels(updated);
  }

  return (
    <Box sx={{ mt: 2, p: 1.5, border: "1px solid", borderColor: "divider", borderRadius: 1 }}>
      <Typography variant="subtitle2" sx={{ mb: 1 }}>
        {t("dimCalendar.title")}
      </Typography>

      {calendarList.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          {t("dimCalendar.noCalendars")}
        </Typography>
      ) : (
        <>
          <FormControl fullWidth size="small" sx={{ mb: 1 }}>
            <InputLabel>{t("dimCalendar.calendarTable")}</InputLabel>
            <Select
              value={selectedCalendarId}
              label={t("dimCalendar.calendarTable")}
              onChange={(e) => handleCalendarChange(e.target.value)}
            >
              <MenuItem value="">{t("dimCalendar.none")}</MenuItem>
              {calendarList.map((c) => (
                <MenuItem key={c.id} value={c.id}>
                  {c.table_name}
                  {c.calendar_type && c.calendar_type !== "standard"
                    ? ` (${c.calendar_type})`
                    : ""}
                </MenuItem>
              ))}
            </Select>
          </FormControl>

          {selectedCalendar && (
            <>
              <FormControlLabel
                control={
                  <Checkbox
                    checked={createAlias}
                    onChange={(e) => setCreateAlias(e.target.checked)}
                    size="small"
                  />
                }
                label={t("dimCalendar.createAlias")}
              />
              {createAlias && (
                <TextField
                  label={t("dimCalendar.aliasName")}
                  size="small"
                  fullWidth
                  value={aliasName}
                  onChange={(e) => setAliasName(e.target.value)}
                  placeholder={t("dimCalendar.aliasPlaceholder")}
                  helperText={t("dimCalendar.aliasHelperText")}
                  sx={{ mb: 1 }}
                />
              )}

              {factTable && factDateColumn && (
                <Box sx={{ mt: 1 }}>
                  <Button
                    size="small"
                    variant="outlined"
                    onClick={handleCheckCoverage}
                    disabled={coverageLoading}
                  >
                    {coverageLoading ? (
                      <CircularProgress size={16} />
                    ) : (
                      t("dimCalendar.checkCoverage")
                    )}
                  </Button>
                  {coverageError && (
                    <Alert severity="error" sx={{ mt: 1 }}>
                      {coverageError}
                    </Alert>
                  )}
                  {coverage && !coverageError && (
                    coverage.covered ? (
                      <Alert severity="success" sx={{ mt: 1 }}>
                        {t("dimCalendar.coverageOk", {
                          calMin: coverage.calendar_min ?? "?",
                          calMax: coverage.calendar_max ?? "?",
                        })}
                      </Alert>
                    ) : (
                      <Alert severity="warning" sx={{ mt: 1 }}>
                        {coverage.warning ??
                          t("dimCalendar.coverageGap", {
                            factMin: coverage.fact_min ?? "?",
                            factMax: coverage.fact_max ?? "?",
                            calMin: coverage.calendar_min ?? "?",
                            calMax: coverage.calendar_max ?? "?",
                          })}
                      </Alert>
                    )
                  )}
                </Box>
              )}

              <Box sx={{ mt: 1 }}>
                <Button
                  size="small"
                  variant="outlined"
                  onClick={() => setShowHierarchy(!showHierarchy)}
                >
                  {showHierarchy ? t("dimCalendar.hideHierarchyLevels") : t("dimCalendar.prePopulateHierarchy")}
                </Button>
              </Box>

              {showHierarchy && (
                <Box sx={{ mt: 1, pl: 1 }}>
                  <Typography variant="caption" color="text.secondary" sx={{ mb: 0.5, display: "block" }}>
                    {t("dimCalendar.levelPickerHint")}
                  </Typography>
                  {(HIERARCHY_PRESETS_RAW[selectedCalendar.calendar_type ?? "standard"] ??
                    HIERARCHY_PRESETS_RAW.standard
                  ).map((level) => (
                    <FormControlLabel
                      key={level}
                      control={
                        <Checkbox
                          size="small"
                          checked={hierarchyLevels.includes(level)}
                          onChange={() => handleLevelToggle(level)}
                        />
                      }
                      label={t(`dimCalendar.level.${level}`)}
                    />
                  ))}
                </Box>
              )}
            </>
          )}
        </>
      )}
    </Box>
  );
}
