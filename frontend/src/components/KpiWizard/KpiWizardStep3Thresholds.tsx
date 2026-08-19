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
import type { KpiThresholdBand } from "../../api/types";
import type { KpiWizardFormState } from "./types";
import { createDefaultPresentationMeta } from "../KpiBusinessBuilder/KpiThresholdEditor";

const DEFAULT_BANDS: KpiThresholdBand[] = [
  { label: "Off Target", color: "#D32F2F", min: 0, max: 50 },
  { label: "Near Target", color: "#F57C00", min: 50, max: 80 },
  { label: "On Track", color: "#388E3C", min: 80, max: 100 },
];

const COLOUR_SWATCHES = [
  "#D32F2F", "#F57C00", "#FBC02D", "#388E3C",
  "#1565C0", "#0D47A1", "#7B1FA2", "#00838F",
  "#E65100", "#757575", "#4E342E", "#9E9E9E",
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
                "&:hover": { transform: "scale(1.15)", transition: "transition 0.1s" },
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
  form: KpiWizardFormState;
  onChange: (patch: Partial<KpiWizardFormState>) => void;
}

export default function KpiWizardStep3Thresholds({ form, onChange }: Props) {
  const t = useT();

  const meta = form.presentation_meta ?? {};
  const direction = form.direction;
  // Bug-6248 / F-017-20: derive the evaluation type from the existing meta;
  // default to the spec basis percentage_of_target so wizard-authored KPIs match
  // seed KPIs (null meta also evaluates as percentage_of_target on the backend).
  const evaluationType: string = meta.evaluation_type ?? "percentage_of_target";
  const bands: KpiThresholdBand[] = meta.bands ?? DEFAULT_BANDS;
  const colorblind = meta.colorblind ?? false;

  const [pickerAnchor, setPickerAnchor] = useState<HTMLElement | null>(null);
  const [pickerBandIdx, setPickerBandIdx] = useState<number>(0);

  // Bug-6248: updateMeta now preserves the current evaluationType instead of
  // forcing absolute_value. Every call writes the type that is already selected
  // (or that the caller explicitly overrides via the patch).
  function updateMeta(patch: Record<string, unknown>) {
    onChange({
      presentation_meta: { ...meta, evaluation_type: evaluationType, ...patch },
    });
  }

  function updateBand(index: number, patch: Partial<KpiThresholdBand>) {
    const updated = bands.map((b, i) => (i === index ? { ...b, ...patch } : b));
    updateMeta({ bands: updated });
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
  }

  function removeBand(index: number) {
    if (bands.length <= 2) return;
    const updated = bands.filter((_, i) => i !== index);
    updateMeta({ bands: updated });
  }

  function moveBand(index: number, dir: -1 | 1) {
    const target = index + dir;
    if (target < 0 || target >= bands.length) return;
    const originalBoundaries = bands.map((b) => ({ min: b.min, max: b.max }));
    const ordered = bands.map((b) => ({ ...b }));
    const [removed] = ordered.splice(index, 1);
    ordered.splice(target, 0, removed);
    for (let i = 0; i < ordered.length; i++) {
      ordered[i].min = originalBoundaries[i].min;
      ordered[i].max = originalBoundaries[i].max;
    }
    updateMeta({ bands: ordered });
  }

  // Parse the target value from the form for default-band scaling.
  const targetNum = form.target_value ? Number(form.target_value) : null;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2.5 }}>
      <Typography variant="h6">{t("kpis.wizard.v2.thresholdsTitle")}</Typography>
      <Typography variant="body2" color="text.secondary">
        {t("kpis.wizard.v2.thresholdsSubtitle")}
      </Typography>

      <FormControl size="small">
        <InputLabel id="kpi-wizard-evaluation-basis-label">
          {t("kpiBusiness.evaluationBasis")}
        </InputLabel>
        <Select
          labelId="kpi-wizard-evaluation-basis-label"
          value={evaluationType}
          label={t("kpiBusiness.evaluationBasis")}
          onChange={(e) => {
            const newType = e.target.value;
            const fresh = createDefaultPresentationMeta(
              newType === "absolute_value" ? (targetNum ?? null) : null,
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
              value={band.min ?? ""}
              onChange={(e) => updateBand(idx, { min: e.target.value === "" ? null : Number(e.target.value) })}
              sx={{ flex: 1 }}
            />
            <TextField
              size="small"
              label={t("kpis.wizard.v2.bandMax")}
              type="number"
              value={band.max ?? ""}
              onChange={(e) => updateBand(idx, { max: e.target.value === "" ? null : Number(e.target.value) })}
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
