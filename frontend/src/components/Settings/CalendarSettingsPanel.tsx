import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Typography,
} from "@mui/material";
import SaveIcon from "@mui/icons-material/Save";
import { calendarSettingsApi } from "../../api/client";
import { safeLocalGet } from "../../utils/safeLocalStorage";
import { useT } from "../../i18n";

export default function CalendarSettingsPanel() {
  const t = useT();
  const tenantId = safeLocalGet("tenant_id", "");
  const qc = useQueryClient();
  const queryKey = ["calendar-settings", tenantId];
  const settingsQuery = useQuery({
    queryKey,
    queryFn: () => calendarSettingsApi.get(tenantId),
    enabled: Boolean(tenantId),
  });
  const [format, setFormat] = useState("");
  useEffect(() => {
    if (settingsQuery.data) setFormat(settingsQuery.data.format);
  }, [settingsQuery.data]);
  const saveMutation = useMutation({
    mutationFn: () => calendarSettingsApi.update(tenantId, format),
    onSuccess: (data) => {
      setFormat(data.format);
      qc.setQueryData(queryKey, data);
    },
  });

  if (settingsQuery.isLoading) {
    return <Box sx={{ p: 3, textAlign: "center" }}><CircularProgress size={24} /></Box>;
  }
  if (settingsQuery.isError) {
    return <Alert severity="error">{t("calendarSettings.loadFailed")}</Alert>;
  }
  const choices = settingsQuery.data?.available_formats ?? [];
  return (
    <Box sx={{ p: 2, maxWidth: 560 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>{t("calendarSettings.title")}</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        {t("calendarSettings.description")}
      </Typography>
      <FormControl fullWidth size="small">
        <InputLabel id="fiscal-year-label-format-label">
          {t("calendarSettings.formatLabel")}
        </InputLabel>
        <Select
          labelId="fiscal-year-label-format-label"
          id="fiscal-year-label-format"
          value={format}
          label={t("calendarSettings.formatLabel")}
          onChange={(event) => setFormat(event.target.value)}
        >
          {choices.map((choice) => <MenuItem key={choice} value={choice}>{choice}</MenuItem>)}
        </Select>
      </FormControl>
      <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
        {t("calendarSettings.rebuildHelp")}
      </Typography>
      {saveMutation.isError && <Alert severity="error" sx={{ mt: 2 }}>{t("calendarSettings.saveFailed")}</Alert>}
      {saveMutation.isSuccess && <Alert severity="success" sx={{ mt: 2 }}>{t("calendarSettings.saved")}</Alert>}
      <Button
        sx={{ mt: 3 }}
        variant="contained"
        startIcon={saveMutation.isPending ? <CircularProgress size={14} /> : <SaveIcon />}
        onClick={() => saveMutation.mutate()}
        disabled={saveMutation.isPending || !format}
      >
        {t("calendarSettings.save")}
      </Button>
    </Box>
  );
}
