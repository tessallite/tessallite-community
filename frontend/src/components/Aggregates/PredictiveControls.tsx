import { useEffect, useState } from "react";
import { useT } from "../../i18n";
import {
  Box,
  Button,
  Card,
  CardContent,
  FormControl,
  FormControlLabel,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Switch,
  TextField,
  Typography,
} from "@mui/material";
import type { EvictionPolicy, ModelUpdate } from "../../api/types";
import { useModel } from "../../api/hooks";

// EVICTION_OPTIONS are built inside the component so labels can be translated.

// F-010-17: storage budget is stored in bytes but entered in human units.
const STORAGE_UNIT_FACTORS: Record<string, number> = {
  b: 1,
  mb: 1024 * 1024,
  gb: 1024 * 1024 * 1024,
};

/** Pick the largest unit that represents `bytes` as a whole number. */
function bytesToUnit(bytes: number | null | undefined): { value: string; unit: string } {
  if (bytes == null) return { value: "", unit: "mb" };
  for (const unit of ["gb", "mb", "b"]) {
    const factor = STORAGE_UNIT_FACTORS[unit];
    if (bytes % factor === 0) {
      return { value: String(bytes / factor), unit };
    }
  }
  return { value: String(bytes), unit: "b" };
}

interface Props {
  model: ReturnType<typeof useModel>["data"] | undefined;
  disabled: boolean;
  onSave: (patch: ModelUpdate) => void;
}

export default function PredictiveControls({ model, disabled, onSave }: Props) {
  const t = useT();
  const EVICTION_OPTIONS: { value: EvictionPolicy; label: string; help: string }[] = [
    {
      value: "predicted_first",
      label: t("predictiveControls.eviction.predictedFirst"),
      help: t("predictiveControls.eviction.predictedFirstHelp"),
    },
    {
      value: "lru",
      label: t("predictiveControls.eviction.lru"),
      help: t("predictiveControls.eviction.lruHelp"),
    },
    {
      value: "validated_survives",
      label: t("predictiveControls.eviction.validatedSurvives"),
      help: t("predictiveControls.eviction.validatedSurvivesHelp"),
    },
    {
      value: "never_evict",
      label: t("predictiveControls.eviction.neverEvict"),
      help: t("predictiveControls.eviction.neverEvictHelp"),
    },
  ];
  const [maxAggregates, setMaxAggregates] = useState("");
  const [storageValue, setStorageValue] = useState("");
  const [storageUnit, setStorageUnit] = useState("mb");
  const [count, setCount] = useState("");
  const [policy, setPolicy] = useState<EvictionPolicy>("predicted_first");
  const [requiresApproval, setRequiresApproval] = useState(false);

  // Convert the entered value + unit back to bytes (null = no limit).
  const storageBytes =
    storageValue.trim() === ""
      ? null
      : Number(storageValue) * (STORAGE_UNIT_FACTORS[storageUnit] ?? 1);

  useEffect(() => {
    if (!model) return;
    setMaxAggregates(
      model.max_aggregates != null ? String(model.max_aggregates) : "",
    );
    const u = bytesToUnit(model.predictive_storage_budget_bytes);
    setStorageValue(u.value);
    setStorageUnit(u.unit);
    setCount(
      model.predictive_storage_budget_count != null
        ? String(model.predictive_storage_budget_count)
        : "",
    );
    setPolicy(model.predictive_eviction_policy ?? "predicted_first");
    setRequiresApproval(Boolean(model.predictive_requires_approval));
  }, [
    model?.id,
    model?.max_aggregates,
    model?.predictive_storage_budget_bytes,
    model?.predictive_storage_budget_count,
    model?.predictive_eviction_policy,
    model?.predictive_requires_approval,
  ]);

  if (!model) return null;

  const dirty =
    maxAggregates !==
      (model.max_aggregates != null ? String(model.max_aggregates) : "") ||
    storageBytes !== (model.predictive_storage_budget_bytes ?? null) ||
    count !==
      (model.predictive_storage_budget_count != null
        ? String(model.predictive_storage_budget_count)
        : "") ||
    policy !== (model.predictive_eviction_policy ?? "predicted_first") ||
    requiresApproval !== Boolean(model.predictive_requires_approval);

  function handleSave() {
    const patch: ModelUpdate = {
      predictive_storage_budget_bytes: storageBytes,
      predictive_storage_budget_count:
        count.trim() === "" ? null : Number(count),
      predictive_eviction_policy: policy,
      predictive_requires_approval: requiresApproval,
    };
    // max_aggregates is a hard cap (>= 1); only send when it is a valid
    // positive integer so a blank/zero field never clears the server value.
    const maxN = Number(maxAggregates);
    if (maxAggregates.trim() !== "" && Number.isFinite(maxN) && maxN >= 1) {
      patch.max_aggregates = maxN;
    }
    onSave(patch);
  }

  return (
    <Card variant="outlined">
      <CardContent sx={{ py: 1.25, "&:last-child": { pb: 1.25 } }}>
        <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
          {t("predictiveControls.title")}
        </Typography>
        <Typography variant="caption" color="text.secondary" display="block" mb={1.5}>
          {t("predictiveControls.description")}
        </Typography>
        <Stack spacing={1.5}>
          <TextField
            size="small"
            label={t("predictiveControls.maxAggregatesLabel")}
            placeholder={t("predictiveControls.maxAggregatesPlaceholder")}
            value={maxAggregates}
            onChange={(e) =>
              setMaxAggregates(e.target.value.replace(/[^0-9]/g, ""))
            }
            disabled={disabled}
            helperText={t("predictiveControls.maxAggregatesHelper")}
            sx={{ maxWidth: { md: 420 } }}
          />
          <Stack direction={{ xs: "column", md: "row" }} spacing={1.5}>
            <Box sx={{ display: "flex", gap: 1, minWidth: 200 }}>
              <TextField
                size="small"
                label={t("predictiveControls.maxStorageLabel")}
                placeholder={t("predictiveControls.maxStoragePlaceholder")}
                value={storageValue}
                onChange={(e) => setStorageValue(e.target.value.replace(/[^0-9]/g, ""))}
                disabled={disabled}
                helperText={t("predictiveControls.maxStorageHelper")}
                sx={{ flexGrow: 1 }}
              />
              <FormControl size="small" disabled={disabled} sx={{ minWidth: 88 }}>
                <InputLabel>{t("predictiveControls.maxStorageUnitLabel")}</InputLabel>
                <Select
                  label={t("predictiveControls.maxStorageUnitLabel")}
                  value={storageUnit}
                  onChange={(e) => setStorageUnit(e.target.value)}
                >
                  <MenuItem value="b">{t("predictiveControls.maxStorageUnit.b")}</MenuItem>
                  <MenuItem value="mb">{t("predictiveControls.maxStorageUnit.mb")}</MenuItem>
                  <MenuItem value="gb">{t("predictiveControls.maxStorageUnit.gb")}</MenuItem>
                </Select>
              </FormControl>
            </Box>
            <TextField
              size="small"
              label={t("predictiveControls.maxCountLabel")}
              placeholder={t("predictiveControls.maxCountPlaceholder")}
              value={count}
              onChange={(e) => setCount(e.target.value.replace(/[^0-9]/g, ""))}
              disabled={disabled}
              helperText={t("predictiveControls.maxCountHelper")}
              sx={{ minWidth: 200 }}
            />
          </Stack>
          <FormControl size="small" fullWidth disabled={disabled}>
            <InputLabel>{t("predictiveControls.whenFullLabel")}</InputLabel>
            <Select
              label={t("predictiveControls.whenFullLabel")}
              value={policy}
              onChange={(e) => setPolicy(e.target.value as EvictionPolicy)}
            >
              {EVICTION_OPTIONS.map((o) => (
                <MenuItem key={o.value} value={o.value} title={o.help}>
                  {o.label}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControlLabel
            control={
              <Switch
                checked={requiresApproval}
                onChange={(e) => setRequiresApproval(e.target.checked)}
                disabled={disabled}
              />
            }
            label={t("predictiveControls.requireApproval")}
          />
          <Box>
            <Button
              size="small"
              variant="contained"
              onClick={handleSave}
              disabled={disabled || !dirty}
            >
              {t("predictiveControls.save")}
            </Button>
          </Box>
        </Stack>
      </CardContent>
    </Card>
  );
}
