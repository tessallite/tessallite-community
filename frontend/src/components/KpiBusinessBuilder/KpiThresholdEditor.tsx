import { useState } from "react";
import {
  Box,
  Button,
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  MenuItem,
  Popover,
  Select,
  Switch,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import DeleteIcon from "@mui/icons-material/Delete";
import ArrowUpwardIcon from "@mui/icons-material/ArrowUpward";
import ArrowDownwardIcon from "@mui/icons-material/ArrowDownward";
import { useT } from "../../i18n";
import type {
  KpiPresentationMeta,
  KpiThresholdBand,
} from "../../api/types_domains/kpis";

// Percentage-of-target bands are stored in ratio scale (0-1) to match backend.
// The UI displays them multiplied by 100 (so users see "80" meaning 80%).
const BANDS_HIGHER: KpiThresholdBand[] = [
  { label: "Off Target", color: "#D32F2F", min: null, max: 0.80 },
  { label: "Near Target", color: "#F57C00", min: 0.80, max: 1.00 },
  { label: "On Track", color: "#388E3C", min: 1.00, max: null },
];

// F-017-20: with the inclusive [min, max) convention, a cost exactly on budget
// (ratio 1.0 against target) fell into the Off Target band — red — which a
// cost-centre owner disputes. Shift the bands so the target sits at the top of
// the amber "Near Target" band (target on budget = amber, not red): On Track
// < 0.9t, Near Target [0.9t, 1.0t], Off Target only when ABOVE budget (> t).
// The 1.0001 boundary keeps ratio 1.0 in Near while anything over budget is Off.
const BANDS_LOWER: KpiThresholdBand[] = [
  { label: "On Track", color: "#388E3C", min: null, max: 0.90 },
  { label: "Near Target", color: "#F57C00", min: 0.90, max: 1.0001 },
  { label: "Off Target", color: "#D32F2F", min: 1.0001, max: null },
];

const BANDS_CLOSER: KpiThresholdBand[] = [
  { label: "Off Target", color: "#D32F2F", min: null, max: 0.80 },
  { label: "Near Target", color: "#F57C00", min: 0.80, max: 0.90 },
  { label: "On Track", color: "#388E3C", min: 0.90, max: null },
];

// F-017-02 / F-017-16: variance evaluation (absolute_variance, percentage_variance)
// matches against RAW DEVIATION (0 = perfect), so bands are deviation-ordered —
// green at the low end, badness increasing upward. Mirrors the backend
// "variance" preset (kpi_threshold.py). For percentage_variance the boundaries
// read as 10% / 20% deviation; for absolute_variance the user retunes them.
const BANDS_VARIANCE: KpiThresholdBand[] = [
  { label: "On Track", color: "#388E3C", min: null, max: 0.10 },
  { label: "Near Target", color: "#F57C00", min: 0.10, max: 0.20 },
  { label: "Off Target", color: "#D32F2F", min: 0.20, max: null },
];

// z_score: bands match against the z-score (direction-normalised so higher =
// better). percentile_rank: bands match against the 0-100 percentile (higher =
// better, inverted for lower_is_better by the backend).
const BANDS_ZSCORE: KpiThresholdBand[] = [
  { label: "Off Target", color: "#D32F2F", min: null, max: -1.0 },
  { label: "Near Target", color: "#F57C00", min: -1.0, max: 1.0 },
  { label: "On Track", color: "#388E3C", min: 1.0, max: null },
];

const BANDS_PERCENTILE: KpiThresholdBand[] = [
  { label: "Off Target", color: "#D32F2F", min: null, max: 33 },
  { label: "Near Target", color: "#F57C00", min: 33, max: 67 },
  { label: "On Track", color: "#388E3C", min: 67, max: null },
];

function defaultBandsForDirection(direction?: string, evaluationType?: string): KpiThresholdBand[] {
  if (evaluationType === "absolute_variance" || evaluationType === "percentage_variance") {
    return BANDS_VARIANCE;
  }
  if (evaluationType === "z_score") return BANDS_ZSCORE;
  if (evaluationType === "percentile_rank") return BANDS_PERCENTILE;
  if (direction === "closer_is_better") return BANDS_CLOSER;
  if (direction === "lower_is_better") {
    // For percentage mode, backend normalises ratio as target/value (high = good),
    // so bands must have On Track at the high end — same order as BANDS_HIGHER.
    if (evaluationType === "percentage_of_target") return BANDS_HIGHER;
    return BANDS_LOWER;
  }
  return BANDS_HIGHER;
}

export function createDefaultPresentationMeta(
  target?: number | null,
  direction?: string,
  forceEvaluationType?: string,
): KpiPresentationMeta {
  const effectiveType = forceEvaluationType
    ?? (direction === "closer_is_better" ? "percentage_of_target" : "absolute_value");
  const baseBands = defaultBandsForDirection(direction, effectiveType);

  if (effectiveType === "percentage_of_target") {
    return { evaluation_type: "percentage_of_target", bands: baseBands };
  }
  // F-017-16: variance / z_score / percentile_rank bands are stored verbatim
  // (deviation, z-score, or percentile scale) — not scaled by target like
  // absolute_value.
  if (
    effectiveType === "absolute_variance" ||
    effectiveType === "percentage_variance" ||
    effectiveType === "z_score" ||
    effectiveType === "percentile_rank"
  ) {
    return { evaluation_type: effectiveType, bands: baseBands };
  }
  // Absolute-value bands: scale ratio-based boundaries (0-1) to target range.
  const max = target && target > 0 ? target : 100;
  return {
    evaluation_type: "absolute_value",
    bands: baseBands.map((band) => ({
      ...band,
      min: band.min === null ? null : Number((band.min * max).toFixed(6)),
      max: band.max === null ? null : Number((band.max * max).toFixed(6)),
    })),
  };
}

const COLOUR_SWATCHES = [
  "#D32F2F",
  "#F57C00",
  "#FBC02D",
  "#388E3C",
  "#1565C0",
  "#0D47A1",
  "#7B1FA2",
  "#00838F",
  "#E65100",
  "#757575",
  "#4E342E",
  "#9E9E9E",
];

function ColourPickerPopover({
  currentColor,
  onSelect,
  anchorEl,
  onClose,
}: {
  currentColor: string;
  onSelect: (color: string) => void;
  anchorEl: HTMLElement | null;
  onClose: () => void;
}) {
  const t = useT();
  const [customHex, setCustomHex] = useState(currentColor);

  return (
    <Popover
      open={Boolean(anchorEl)}
      anchorEl={anchorEl}
      onClose={onClose}
      anchorOrigin={{ vertical: "bottom", horizontal: "left" }}
      transformOrigin={{ vertical: "top", horizontal: "left" }}
    >
      <Box sx={{ p: 1.5, width: 200 }}>
        <Box sx={{ display: "flex", flexWrap: "wrap", gap: 0.75, mb: 1.5 }}>
          {COLOUR_SWATCHES.map((c) => (
            <Box
              key={c}
              onClick={() => { onSelect(c); onClose(); }}
              sx={{
                width: 28,
                height: 28,
                borderRadius: "4px",
                backgroundColor: c,
                border: c === currentColor ? "2px solid #000" : "1px solid rgba(0,0,0,0.15)",
                cursor: "pointer",
                "&:hover": { transform: "scale(1.15)", transition: "transform 0.1s" },
              }}
            />
          ))}
        </Box>
        <Box sx={{ display: "flex", gap: 0.5, alignItems: "center" }}>
          <TextField
            size="small"
            label={t("kpis.wizard.v2.customHex")}
            value={customHex}
            onChange={(e) => setCustomHex(e.target.value)}
            inputProps={{ maxLength: 7 }}
            sx={{ flex: 1, "& input": { fontFamily: "monospace", fontSize: 13 } }}
          />
          <Button
            size="small"
            variant="outlined"
            disabled={!/^#[0-9A-Fa-f]{6}$/.test(customHex)}
            onClick={() => { onSelect(customHex); onClose(); }}
          >
            OK
          </Button>
        </Box>
      </Box>
    </Popover>
  );
}

interface Props {
  meta: KpiPresentationMeta | null;
  direction: string;
  target?: number | null;
  onChange: (meta: KpiPresentationMeta) => void;
  onBandsCustomized?: () => void;
}

export function KpiThresholdEditor({ meta, direction, target, onChange, onBandsCustomized }: Props) {
  const t = useT();

  const raw = meta ?? createDefaultPresentationMeta(null, direction);
  // Coerce closer_is_better + absolute_value → percentage_of_target on load
  // so existing/imported data self-heals and the Select is never out-of-range.
  const m = (direction === "closer_is_better" && raw.evaluation_type === "absolute_value")
    ? { ...raw, ...createDefaultPresentationMeta(null, direction, "percentage_of_target") }
    : raw;
  const evaluationType = m.evaluation_type ?? "absolute_value";
  const bands: KpiThresholdBand[] = m.bands ?? defaultBandsForDirection(direction, evaluationType);
  const colorblind = m.colorblind ?? false;

  const [pickerAnchor, setPickerAnchor] = useState<HTMLElement | null>(null);
  const [pickerBandIdx, setPickerBandIdx] = useState<number>(0);

  function updateMeta(patch: Record<string, unknown>) {
    onChange({ ...m, evaluation_type: evaluationType, ...patch });
  }

  function updateBand(index: number, patch: Partial<KpiThresholdBand>) {
    const updated = bands.map((b, i) => (i === index ? { ...b, ...patch } : b));
    updateMeta({ bands: updated });
    onBandsCustomized?.();
  }

  function addBand() {
    if (bands.length >= 5) return;
    const last = bands[bands.length - 1];
    const increment = evaluationType === "percentage_of_target" ? 0.2 : 20;
    const newBand: KpiThresholdBand = {
      label: "",
      color: "#9E9E9E",
      min: last.max ?? 0,
      max: (last.max ?? 0) + increment,
    };
    updateMeta({ bands: [...bands, newBand] });
    onBandsCustomized?.();
  }

  function removeBand(index: number) {
    if (bands.length <= 2) return;
    const updated = bands.filter((_, i) => i !== index);
    updateMeta({ bands: updated });
    onBandsCustomized?.();
  }

  function moveBand(index: number, dir: -1 | 1) {
    const dest = index + dir;
    if (dest < 0 || dest >= bands.length) return;
    const originalBoundaries = bands.map((b) => ({ min: b.min, max: b.max }));
    const ordered = bands.map((b) => ({ ...b }));
    const [removed] = ordered.splice(index, 1);
    ordered.splice(dest, 0, removed);
    for (let i = 0; i < ordered.length; i++) {
      ordered[i].min = originalBoundaries[i].min;
      ordered[i].max = originalBoundaries[i].max;
    }
    updateMeta({ bands: ordered });
    onBandsCustomized?.();
  }

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 1.5 }}>
      <Typography variant="subtitle2">{t("kpiBusiness.thresholdsTitle")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("kpiBusiness.thresholdsHint")}
      </Typography>

      <FormControl size="small">
        <InputLabel id="kpi-business-evaluation-basis-label">
          {t("kpiBusiness.evaluationBasis")}
        </InputLabel>
        <Select
          labelId="kpi-business-evaluation-basis-label"
          value={evaluationType}
          label={t("kpiBusiness.evaluationBasis")}
          onChange={(e) => {
            const newType = e.target.value;
            const fresh = createDefaultPresentationMeta(
              newType === "absolute_value" ? (target ?? null) : null,
              direction,
              newType,
            );
            updateMeta({ evaluation_type: newType, bands: fresh.bands });
          }}
        >
          {direction !== "closer_is_better" && (
            <MenuItem value="absolute_value">
              {t("kpiBusiness.evaluationBasisValue")}
            </MenuItem>
          )}
          <MenuItem value="percentage_of_target">
            {t("kpiBusiness.evaluationBasisTargetPct")}
          </MenuItem>
          {/* F-017-16: variance / statistical / percentile evaluation types are
              now reachable from the UI (variance fixed by F-017-02; z_score and
              percentile_rank fixed by F-017-03). */}
          <MenuItem value="absolute_variance">
            {t("kpiBusiness.evaluationBasisAbsVariance")}
          </MenuItem>
          <MenuItem value="percentage_variance">
            {t("kpiBusiness.evaluationBasisPctVariance")}
          </MenuItem>
          <MenuItem value="z_score">
            {t("kpiBusiness.evaluationBasisZScore")}
          </MenuItem>
          <MenuItem value="percentile_rank">
            {t("kpiBusiness.evaluationBasisPercentile")}
          </MenuItem>
        </Select>
      </FormControl>

      {/* F-017-16: percentile_rank ranks the value against peers grouped by this
          dimension (presentation_meta.peer_dimension, consumed server-side). */}
      {evaluationType === "percentile_rank" && (
        <TextField
          size="small"
          label={t("kpiBusiness.peerDimension")}
          helperText={t("kpiBusiness.peerDimensionHint")}
          value={m.peer_dimension ?? ""}
          onChange={(e) => updateMeta({ peer_dimension: e.target.value })}
        />
      )}

      <Box sx={{ display: "flex", flexDirection: "column", gap: 1 }}>
        {bands.map((band, idx) => (
          <Box key={idx} sx={{ display: "flex", gap: 1, alignItems: "center" }}>
            <Tooltip title={t("kpis.wizard.v2.pickColor")}>
              <Box
                onClick={(e) => { setPickerBandIdx(idx); setPickerAnchor(e.currentTarget); }}
                sx={{
                  width: 28,
                  height: 28,
                  borderRadius: "4px",
                  backgroundColor: band.color,
                  border: "1px solid rgba(0,0,0,0.12)",
                  flexShrink: 0,
                  cursor: "pointer",
                  "&:hover": { boxShadow: "0 0 0 2px rgba(0,0,0,0.25)" },
                }}
              />
            </Tooltip>
            <TextField
              size="small"
              label={t("kpis.wizard.v2.bandLabel")}
              value={band.label}
              onChange={(e) => updateBand(idx, { label: e.target.value })}
              sx={{ flex: 2 }}
            />
            <TextField
              size="small"
              label={t("kpis.wizard.v2.bandMin")}
              type="number"
              value={band.min === null ? "" : (evaluationType === "percentage_of_target" ? +(band.min * 100).toFixed(2) : band.min)}
              onChange={(e) => {
                const raw = e.target.value === "" ? null : Number(e.target.value);
                const stored = raw === null ? null : (evaluationType === "percentage_of_target" ? raw / 100 : raw);
                updateBand(idx, { min: stored });
              }}
              sx={{ flex: 1 }}
            />
            <TextField
              size="small"
              label={t("kpis.wizard.v2.bandMax")}
              type="number"
              value={band.max === null ? "" : (evaluationType === "percentage_of_target" ? +(band.max * 100).toFixed(2) : band.max)}
              onChange={(e) => {
                const raw = e.target.value === "" ? null : Number(e.target.value);
                const stored = raw === null ? null : (evaluationType === "percentage_of_target" ? raw / 100 : raw);
                updateBand(idx, { max: stored });
              }}
              sx={{ flex: 1 }}
            />
            <IconButton
              size="small"
              onClick={() => moveBand(idx, -1)}
              disabled={idx === 0}
              aria-label={t("kpis.wizard.v2.moveBandUp")}
            >
              <ArrowUpwardIcon fontSize="small" />
            </IconButton>
            <IconButton
              size="small"
              onClick={() => moveBand(idx, 1)}
              disabled={idx === bands.length - 1}
              aria-label={t("kpis.wizard.v2.moveBandDown")}
            >
              <ArrowDownwardIcon fontSize="small" />
            </IconButton>
            <IconButton
              size="small"
              onClick={() => removeBand(idx)}
              disabled={bands.length <= 2}
              aria-label={t("kpis.wizard.v2.removeBand")}
            >
              <DeleteIcon fontSize="small" />
            </IconButton>
          </Box>
        ))}
      </Box>

      <Button
        size="small"
        startIcon={<AddIcon />}
        onClick={addBand}
        disabled={bands.length >= 5}
        sx={{ alignSelf: "flex-start" }}
      >
        {t("kpis.wizard.v2.addBand")}
      </Button>

      <FormControlLabel
        control={
          <Switch
            size="small"
            checked={colorblind}
            onChange={(e) => updateMeta({ colorblind: e.target.checked })}
          />
        }
        label={t("kpis.wizard.v2.colorblindMode")}
      />

      <ColourPickerPopover
        currentColor={bands[pickerBandIdx]?.color ?? "#9E9E9E"}
        anchorEl={pickerAnchor}
        onClose={() => setPickerAnchor(null)}
        onSelect={(c) => updateBand(pickerBandIdx, { color: c })}
      />
    </Box>
  );
}
