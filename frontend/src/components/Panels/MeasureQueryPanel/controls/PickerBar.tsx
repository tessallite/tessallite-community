import { useState } from "react";
import {
  Button,
  FormControl,
  IconButton,
  Menu,
  MenuItem,
  Select,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import CloseIcon from "@mui/icons-material/Close";
import KeyboardArrowUpIcon from "@mui/icons-material/KeyboardArrowUp";
import KeyboardArrowDownIcon from "@mui/icons-material/KeyboardArrowDown";
import TuneIcon from "@mui/icons-material/Tune";
import type { Dimension, Measure } from "../../../../api/types";
import DimChipList from "./DimChipList";
import ExecutionModeDialog, { type ExecutionMode } from "./ExecutionModeDialog";
import FormatPopover from "./FormatPopover";
import { PIVOT_MAX_COL_DIMS, PIVOT_MAX_ROW_DIMS } from "../types";
import { AGG_OPTIONS, RECORD_COUNT_ID, type MeasureSel } from "../measureColumns";
import { useT } from "../../../../i18n";

type Props = {
  projectId: string;
  modelId: string;
  // Available base measures (already includes the synthetic Record Count).
  measures: Measure[];
  dimensions: Dimension[];
  selections: MeasureSel[];
  rowDimIds: string[];
  colDimIds: string[];
  executing: boolean;
  forceLive: boolean;
  runDisabled: boolean;
  disabledDimensionReasons?: Record<string, string>;
  onSelectionsChange: (next: MeasureSel[]) => void;
  onRowDimsChange: (ids: string[]) => void;
  onColDimsChange: (ids: string[]) => void;
  onForceLiveChange: (v: boolean) => void;
  onRun: () => void;
  hideRunButton?: boolean;
};

export default function PickerBar({
  projectId,
  modelId,
  measures,
  dimensions,
  selections,
  rowDimIds,
  colDimIds,
  executing,
  forceLive,
  runDisabled,
  disabledDimensionReasons = {},
  onSelectionsChange,
  onRowDimsChange,
  onColDimsChange,
  onForceLiveChange,
  onRun,
  hideRunButton = false,
}: Props) {
  const t = useT();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [addAnchor, setAddAnchor] = useState<HTMLElement | null>(null);
  const mode: ExecutionMode = forceLive ? "force_live" : "auto";

  const measuresById = new Map(measures.map((m) => [m.id, m]));

  function addMeasure(base: Measure) {
    const agg = base.id === RECORD_COUNT_ID
      ? "COUNT"
      : (base.default_agg || "SUM").toUpperCase();
    onSelectionsChange([...selections, { measureId: base.id, agg }]);
    setAddAnchor(null);
  }

  function setAgg(idx: number, agg: string) {
    const next = selections.map((s, i) => (i === idx ? { ...s, agg } : s));
    onSelectionsChange(next);
  }

  function moveSelection(from: number, to: number) {
    if (to < 0 || to >= selections.length) return;
    const next = [...selections];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    onSelectionsChange(next);
  }

  function removeSelection(idx: number) {
    onSelectionsChange(selections.filter((_, i) => i !== idx));
  }

  return (
    <Stack spacing={0.75}>
      {/* Measures: add control + plain-text selected rows */}
      <Stack spacing={0.5}>
        <Stack direction="row" spacing={0.75} sx={{ alignItems: "center", flexWrap: "wrap" }}>
          <Typography variant="caption" sx={{ fontWeight: 700, color: "text.secondary", mr: 0.5 }}>
            {t("pickerBar.measuresLabel")}:
          </Typography>
          {selections.length === 0 && (
            <Typography variant="caption" color="text.secondary">
              {t("pickerBar.none")}
            </Typography>
          )}
          <Button
            size="small"
            variant="outlined"
            startIcon={<AddIcon fontSize="small" />}
            disabled={measures.length === 0}
            onClick={(e) => setAddAnchor(e.currentTarget)}
            sx={{ py: 0.1, minWidth: 0 }}
          >
            {t("pickerBar.addMeasure")}
          </Button>
          <Menu
            anchorEl={addAnchor}
            open={Boolean(addAnchor)}
            onClose={() => setAddAnchor(null)}
          >
            {measures.map((m) => (
              <MenuItem key={m.id} onClick={() => addMeasure(m)}>
                <Stack direction="row" alignItems="center" spacing={0.75}>
                  <span>{m.display_name || m.name}</span>
                  {m.measure_type === "calculated" && (
                    <Typography variant="caption" color="text.secondary">
                      ({t("pickerBar.formula")})
                    </Typography>
                  )}
                  {m.variant_kind && (
                    <Typography variant="caption" color="text.secondary">
                      ({m.variant_kind})
                    </Typography>
                  )}
                </Stack>
              </MenuItem>
            ))}
          </Menu>
        </Stack>

        {selections.map((sel, idx) => {
          const m = measuresById.get(sel.measureId);
          if (!m) return null;
          const isRecordCount = m.id === RECORD_COUNT_ID;
          const isScratchpad = (m as { _scratchpad?: boolean })._scratchpad === true;
          const aggLocked = isRecordCount || isScratchpad;
          const currentAgg = (sel.agg || m.default_agg || "SUM").toUpperCase();
          return (
            <Stack
              key={`${sel.measureId}-${idx}`}
              direction="row"
              alignItems="center"
              spacing={0.5}
              sx={{ flexWrap: "wrap" }}
            >
              <Typography variant="caption" sx={{ minWidth: 16, color: "text.secondary" }}>
                {idx + 1}.
              </Typography>
              <Typography
                variant="caption"
                sx={{ maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
              >
                {m.display_name || m.name}
              </Typography>
              {aggLocked ? (
                <Typography variant="caption" color="text.secondary">
                  {t(`pivot.agg.${currentAgg.toLowerCase()}`)}
                </Typography>
              ) : (
                <FormControl size="small" sx={{ minWidth: 96 }}>
                  <Select
                    value={currentAgg}
                    onChange={(e) => setAgg(idx, e.target.value as string)}
                    sx={{ fontSize: 11, "& .MuiSelect-select": { py: 0.25 } }}
                  >
                    {AGG_OPTIONS.map((agg) => (
                      <MenuItem key={agg} value={agg} sx={{ fontSize: 12 }}>
                        {t(`pivot.agg.${agg.toLowerCase()}`)}
                      </MenuItem>
                    ))}
                  </Select>
                </FormControl>
              )}
              {!aggLocked && (
                <FormatPopover projectId={projectId} modelId={modelId} measure={m} />
              )}
              <Tooltip title={t("pickerBar.moveUp")}>
                <span>
                  <IconButton
                    size="small"
                    disabled={idx === 0}
                    onClick={() => moveSelection(idx, idx - 1)}
                    sx={{ width: 18, height: 18 }}
                  >
                    <KeyboardArrowUpIcon fontSize="inherit" />
                  </IconButton>
                </span>
              </Tooltip>
              <Tooltip title={t("pickerBar.moveDown")}>
                <span>
                  <IconButton
                    size="small"
                    disabled={idx === selections.length - 1}
                    onClick={() => moveSelection(idx, idx + 1)}
                    sx={{ width: 18, height: 18 }}
                  >
                    <KeyboardArrowDownIcon fontSize="inherit" />
                  </IconButton>
                </span>
              </Tooltip>
              <Tooltip title={t("pickerBar.remove")}>
                <IconButton
                  size="small"
                  onClick={() => removeSelection(idx)}
                  sx={{ width: 18, height: 18 }}
                >
                  <CloseIcon fontSize="inherit" />
                </IconButton>
              </Tooltip>
            </Stack>
          );
        })}
      </Stack>

      <Stack direction="row" spacing={2} sx={{ alignItems: "flex-start", flexWrap: "wrap" }}>
        <DimChipList
          label={t("pickerBar.rowsLabel")}
          available={dimensions}
          selectedIds={rowDimIds}
          max={PIVOT_MAX_ROW_DIMS}
          disabledReasons={disabledDimensionReasons}
          onChange={onRowDimsChange}
        />
        <DimChipList
          label={t("pickerBar.columnsLabel")}
          available={dimensions.filter((d) => !rowDimIds.includes(d.id))}
          selectedIds={colDimIds}
          max={PIVOT_MAX_COL_DIMS}
          disabledReasons={disabledDimensionReasons}
          onChange={onColDimsChange}
        />
      </Stack>

      <Stack direction="row" alignItems="center" gap={1}>
        <Button
          size="small"
          variant="outlined"
          startIcon={<TuneIcon fontSize="small" />}
          onClick={() => setDialogOpen(true)}
        >
          {t("pickerBar.executionMode")}
        </Button>
        {forceLive && (
          <Typography variant="caption" sx={{ fontWeight: 700, color: "warning.main" }}>
            {t("pickerBar.forceLive")}
          </Typography>
        )}
        {!hideRunButton && (
          <Button
            variant="contained"
            size="small"
            disabled={runDisabled}
            onClick={onRun}
          >
            {executing ? t("pickerBar.running") : t("pickerBar.run")}
          </Button>
        )}
      </Stack>

      <ExecutionModeDialog
        open={dialogOpen}
        value={mode}
        onChange={(m) => onForceLiveChange(m === "force_live")}
        onClose={() => setDialogOpen(false)}
      />
    </Stack>
  );
}
