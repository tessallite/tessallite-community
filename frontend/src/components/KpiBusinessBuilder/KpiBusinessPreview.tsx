import { useState } from "react";
import {
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Collapse,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import CodeIcon from "@mui/icons-material/Code";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import WarningIcon from "@mui/icons-material/Warning";
import ErrorIcon from "@mui/icons-material/Error";
import TrendingUpIcon from "@mui/icons-material/TrendingUp";
import TrendingDownIcon from "@mui/icons-material/TrendingDown";
import TrendingFlatIcon from "@mui/icons-material/TrendingFlat";

import { useT } from "../../i18n";
import type { KpiEvaluateResponse } from "../../api/types_domains/kpis";

type Props = {
  result: KpiEvaluateResponse | null;
  loading: boolean;
  onPreview: () => void;
};

function StatusChip({ status, label }: { status: number | null; label: string | null }) {
  if (status === null || !label) return null;
  const icon =
    status >= 1 ? (
      <CheckCircleIcon fontSize="small" />
    ) : status === 0 ? (
      <WarningIcon fontSize="small" />
    ) : (
      <ErrorIcon fontSize="small" />
    );
  const color: "success" | "warning" | "error" =
    status >= 1 ? "success" : status === 0 ? "warning" : "error";
  return <Chip size="small" icon={icon} label={label} color={color} variant="outlined" />;
}

function TrendChip({ trend, label }: { trend: number | null; label: string | null }) {
  if (trend === null || !label) return null;
  const icon =
    trend >= 1 ? (
      <TrendingUpIcon fontSize="small" />
    ) : trend === 0 ? (
      <TrendingFlatIcon fontSize="small" />
    ) : (
      <TrendingDownIcon fontSize="small" />
    );
  return <Chip size="small" icon={icon} label={label} variant="outlined" />;
}

export function KpiBusinessPreview({ result, loading, onPreview }: Props) {
  const t = useT();
  const [showSql, setShowSql] = useState(false);

  return (
    <Box>
      <Stack direction="row" spacing={1}>
        <Button
          size="small"
          variant="outlined"
          startIcon={loading ? <CircularProgress size={14} /> : <PlayArrowIcon />}
          onClick={onPreview}
          disabled={loading}
        >
          {t("kpiBusiness.preview")}
        </Button>
        {result?.compiled_sql && !loading && (
          <Button
            size="small"
            variant="text"
            startIcon={<CodeIcon />}
            onClick={() => setShowSql(!showSql)}
          >
            {showSql ? t("kpiBusiness.hideSql") : t("kpiBusiness.showSql")}
          </Button>
        )}
      </Stack>

      {result && !loading && (
        <Card variant="outlined" sx={{ mt: 1.5 }}>
          <CardContent sx={{ py: 1.5, "&:last-child": { pb: 1.5 } }}>
            <Stack spacing={1}>
              <Stack direction="row" spacing={2} alignItems="baseline" flexWrap="wrap">
                {result.formatted_value && (
                  <Typography variant="h5" fontWeight={700}>
                    {result.formatted_value}
                  </Typography>
                )}
                {result.value !== null && !result.formatted_value && (
                  <Typography variant="h5" fontWeight={700}>
                    {result.value}
                  </Typography>
                )}
                {result.formatted_target && (
                  <Typography variant="body2" color="text.secondary">
                    {t("kpiBusiness.target")}: {result.formatted_target}
                  </Typography>
                )}
                {result.formatted_variance && (
                  <Typography variant="body2" color="text.secondary">
                    {result.formatted_variance}
                  </Typography>
                )}
              </Stack>

              <Stack direction="row" spacing={1} flexWrap="wrap">
                <StatusChip status={result.status} label={result.status_label} />
                <TrendChip trend={result.trend} label={result.trend_label} />
                {/* F-017-02 (Opus-R1): render the direction-normalised percent
                    so the sign matches the improving/declining intent, matching
                    the scorecard chip fix. Fall back to raw for legacy responses. */}
                {(() => {
                  const pct = result.trend_pct_normalised ?? result.trend_pct;
                  return pct !== null && pct !== undefined ? (
                    <Chip
                      size="small"
                      label={`${pct > 0 ? "+" : ""}${(pct * 100).toFixed(1)}%`}
                      variant="outlined"
                    />
                  ) : null;
                })()}
              </Stack>

              <Collapse in={showSql}>
                <TextField
                  fullWidth
                  size="small"
                  multiline
                  minRows={2}
                  value={result.compiled_sql || ""}
                  InputProps={{ readOnly: true }}
                  sx={{
                    mt: 1,
                    "& textarea": { fontFamily: "monospace", fontSize: 12 },
                  }}
                />
              </Collapse>

              {result.evaluation_ms !== null && (
                <Typography variant="caption" color="text.secondary">
                  {t("kpiBusiness.evaluatedIn", { ms: String(result.evaluation_ms) })}
                </Typography>
              )}
            </Stack>
          </CardContent>
        </Card>
      )}
    </Box>
  );
}
