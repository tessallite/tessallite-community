import { useState } from "react";
import { useT } from "../../i18n";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  CircularProgress,
  FormControlLabel,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { schedulerApiClient } from "../../api/client";

interface Props {
  projectId: string;
  modelId: string;
}

export function SLAConfigPanel({ projectId, modelId }: Props) {
  const t = useT();
  const qc = useQueryClient();
  const key = ["slaConfig", projectId, modelId];

  const { data, isLoading } = useQuery({
    queryKey: key,
    queryFn: () => schedulerApiClient.getSLA(projectId, modelId).catch(() => null),
    enabled: !!projectId && !!modelId,
  });

  const [targetTime, setTargetTime] = useState("");
  const [gracePeriod, setGracePeriod] = useState(15);
  const [maxRetries, setMaxRetries] = useState(1);
  const [alertOnBreach, setAlertOnBreach] = useState(true);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const saveEnabled = !data || editing;

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (data) {
        return schedulerApiClient.updateSLA(projectId, modelId, {
          target_completion_time: targetTime,
          grace_period_minutes: gracePeriod,
          max_retries: maxRetries,
          alert_on_breach: alertOnBreach,
        });
      }
      return schedulerApiClient.createSLA(projectId, modelId, {
        target_completion_time: targetTime,
        grace_period_minutes: gracePeriod,
        max_retries: maxRetries,
        alert_on_breach: alertOnBreach,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: key });
      setEditing(false);
      setError(null);
    },
    onError: (e: any) => {
      setError(e?.response?.data?.detail ?? t("sla.failedToSave"));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => schedulerApiClient.deleteSLA(projectId, modelId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: key });
      setEditing(false);
    },
    onError: (e: any) => {
      setError(e?.response?.data?.detail ?? t("sla.failedToDelete"));
    },
  });

  const startEdit = () => {
    if (data) {
      setTargetTime(data.target_completion_time);
      setGracePeriod(data.grace_period_minutes);
      setMaxRetries(data.max_retries);
      setAlertOnBreach(data.alert_on_breach);
    } else {
      setTargetTime(t("sla.timePlaceholder"));
      setGracePeriod(15);
      setMaxRetries(1);
      setAlertOnBreach(true);
    }
    setEditing(true);
    setError(null);
  };

  if (isLoading) return <Box sx={{ p: 2 }}><CircularProgress size={18} /></Box>;

  return (
    <Stack spacing={2} sx={{ p: 2 }}>
      <Typography variant="subtitle2">{t("sla.title")}</Typography>
      {data && !editing ? (
        <Stack spacing={1}>
          <Typography variant="body2">
            {t("sla.target", { time: data.target_completion_time })} &nbsp;|&nbsp;
            {t("sla.grace", { minutes: String(data.grace_period_minutes) })} &nbsp;|&nbsp;
            {t("sla.maxRetries", { count: String(data.max_retries) })} &nbsp;|&nbsp;
            {t("sla.alertOnBreach")}: {data.alert_on_breach ? t("sla.yes") : t("sla.no")}
          </Typography>
          <Stack direction="row" spacing={1}>
            <Button size="small" variant="outlined" onClick={startEdit}>{t("sla.edit")}</Button>
            <Button
              size="small"
              variant="outlined"
              onClick={() => deleteMutation.mutate()}
              disabled={deleteMutation.isPending}
            >
              {t("sla.remove")}
            </Button>
          </Stack>
        </Stack>
      ) : editing ? (
        <Stack spacing={2}>
          <TextField
            label={t("sla.targetCompletionTime")}
            value={targetTime}
            onChange={(e) => setTargetTime(e.target.value)}
            size="small"
            placeholder={t("sla.timePlaceholder")}
            inputProps={{ pattern: "[0-2][0-9]:[0-5][0-9]" }}
          />
          <TextField
            label={t("sla.gracePeriod")}
            type="number"
            value={gracePeriod}
            onChange={(e) => setGracePeriod(Number(e.target.value))}
            size="small"
            inputProps={{ min: 0, max: 1440 }}
          />
          <TextField
            label={t("sla.maxRetriesLabel")}
            type="number"
            value={maxRetries}
            onChange={(e) => setMaxRetries(Number(e.target.value))}
            size="small"
            inputProps={{ min: 0, max: 10 }}
          />
          <FormControlLabel
            control={
              <Checkbox
                checked={alertOnBreach}
                onChange={(e) => setAlertOnBreach(e.target.checked)}
                size="small"
              />
            }
            label={t("sla.alertOnBreach")}
          />
          {error && <Alert severity="error" sx={{ py: 0 }}>{error}</Alert>}
          <Stack direction="row" spacing={1}>
            <Button
              size="small"
              variant="contained"
              onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending || !targetTime}
            >
              {saveMutation.isPending ? <CircularProgress size={14} /> : t("sla.save")}
            </Button>
            <Button size="small" onClick={() => setEditing(false)}>{t("sla.cancel")}</Button>
          </Stack>
        </Stack>
      ) : (
        <Stack spacing={1}>
          <Typography variant="body2" color="text.secondary">
            {t("sla.noSla")}
          </Typography>
          <Button size="small" variant="outlined" onClick={startEdit} sx={{ alignSelf: "flex-start" }}>
            {t("sla.configureSla")}
          </Button>
        </Stack>
      )}
    </Stack>
  );
}
