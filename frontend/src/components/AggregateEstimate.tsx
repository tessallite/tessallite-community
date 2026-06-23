/**
 * AggregateEstimate — shown in AggregatePolicyView during aggregate creation.
 *
 * Displays:
 *   - Grain selection (dimensions chosen)
 *   - ROI gate pass/fail indicators
 *   - Warning for non-additive measures requiring exact grain
 */
import { useT } from "../i18n";
import {
  Alert,
  Box,
  Divider,
  LinearProgress,
  Stack,
  Typography,
} from "@mui/material";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import WarningIcon from "@mui/icons-material/Warning";
import type { Measure } from "../api/types";

interface Props {
  selectedDimensions: string[];
  selectedMeasures: Measure[];
  includeQuantiles: boolean;
  includeStats?: boolean;
}

export default function AggregateEstimate({
  selectedDimensions,
  selectedMeasures,
  includeQuantiles,
  includeStats = false,
}: Props) {
  const t = useT();
  const nonAdditive = selectedMeasures.filter((m) => !m.is_additive);
  const hasNonAdditive = nonAdditive.length > 0;

  // Rough ROI score: more dimensions = lower selectivity
  const selectivityBonus = Math.max(0, 1.0 - (selectedDimensions.length - 1) * 0.1);
  const measureBonus = Math.min(0.4, (selectedMeasures.length - 1) * 0.1);
  const roiScore = Math.min(1.0, (1.0 + selectivityBonus + measureBonus) / 2.4);

  const grainOk = selectedDimensions.length >= 1;
  const measureOk = selectedMeasures.length >= 1;

  return (
    <Box sx={{ p: 2, border: "1px solid", borderColor: "divider", borderRadius: 1, bgcolor: "grey.50" }}>
      <Typography variant="subtitle2" fontWeight={700} mb={1.5}>
        {t("aggEstimate.title")}
      </Typography>

      <Stack spacing={1}>
        <Box display="flex" alignItems="center" gap={1}>
          {grainOk ? <CheckCircleIcon color="success" fontSize="small" /> : <WarningIcon color="warning" fontSize="small" />}
          <Typography variant="body2">
            {t("aggEstimate.grain", {
              dims:
                selectedDimensions.length > 0
                  ? selectedDimensions.join(", ")
                  : t("aggEstimate.noneSelected"),
            })}
          </Typography>
        </Box>

        <Box display="flex" alignItems="center" gap={1}>
          {measureOk ? <CheckCircleIcon color="success" fontSize="small" /> : <WarningIcon color="warning" fontSize="small" />}
          <Typography variant="body2">
            {t("aggEstimate.measures", {
              measures:
                selectedMeasures.length > 0
                  ? selectedMeasures.map((m) => m.name).join(", ")
                  : t("aggEstimate.noneSelected"),
            })}
          </Typography>
        </Box>

        {includeQuantiles && (
          <Box display="flex" alignItems="center" gap={1}>
            <CheckCircleIcon color="info" fontSize="small" />
            <Typography variant="body2">{t("aggEstimate.quantileNote")}</Typography>
          </Box>
        )}

        {includeStats && (
          <Box display="flex" alignItems="center" gap={1}>
            <CheckCircleIcon color="info" fontSize="small" />
            <Typography variant="body2">{t("aggEstimate.statsNote")}</Typography>
          </Box>
        )}
      </Stack>

      <Divider sx={{ my: 1.5 }} />

      <Typography variant="caption" color="text.secondary" display="block" mb={0.5}>
        {t("aggEstimate.estimatedRoi")}
      </Typography>
      <Box display="flex" alignItems="center" gap={1}>
        <LinearProgress
          variant="determinate"
          value={roiScore * 100}
          sx={{ flexGrow: 1, height: 8, borderRadius: 4 }}
          color={roiScore > 0.6 ? "success" : roiScore > 0.3 ? "warning" : "error"}
        />
        <Typography variant="body2" fontWeight={600}>
          {Math.round(roiScore * 100)}%
        </Typography>
      </Box>

      {hasNonAdditive && (
        <Alert severity="warning" icon={<WarningIcon />} sx={{ mt: 1.5 }}>
          {t("aggEstimate.exactGrainForNonAdditive", {
            measures: nonAdditive.map((m) => m.name).join(", "),
          })}
        </Alert>
      )}
    </Box>
  );
}
