import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  TextField,
  Typography,
} from "@mui/material";
import SaveIcon from "@mui/icons-material/Save";
import { brandingApi, type BrandingConfig } from "../../api/client";
import { safeLocalGet } from "../../utils/safeLocalStorage";
import { BRANDING_CHANGED_EVENT } from "../../utils/brandingEvents";
import { useT } from "../../i18n";

export default function BrandingPanel() {
  const t = useT();
  const tenantId = safeLocalGet("tenant_id", "");
  const qc = useQueryClient();
  const queryKey = ["branding", tenantId];

  const brandingQuery = useQuery({
    queryKey,
    queryFn: () => brandingApi.get(tenantId),
    enabled: Boolean(tenantId),
  });

  const [form, setForm] = useState<BrandingConfig>({
    logo_url: null,
    primary_color: null,
    secondary_color: null,
    font_family: null,
    app_title: null,
  });

  useEffect(() => {
    if (brandingQuery.data) setForm(brandingQuery.data);
  }, [brandingQuery.data]);

  const saveMut = useMutation({
    mutationFn: () => brandingApi.update(tenantId, form),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey });
      window.dispatchEvent(new Event(BRANDING_CHANGED_EVENT));
    },
  });

  if (brandingQuery.isLoading) {
    return (
      <Box sx={{ p: 3, textAlign: "center" }}>
        <CircularProgress size={24} />
      </Box>
    );
  }

  return (
    <Box sx={{ p: 2, maxWidth: 520 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        {t("branding.title")}
      </Typography>

      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        {t("branding.description")}
      </Typography>

      <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <TextField
          label={t("branding.appTitle")}
          size="small"
          value={form.app_title ?? ""}
          onChange={(e) =>
            setForm({ ...form, app_title: e.target.value || null })
          }
          helperText={t("branding.appTitleHelp")}
        />
        <TextField
          label={t("branding.logoUrl")}
          size="small"
          value={form.logo_url ?? ""}
          onChange={(e) =>
            setForm({ ...form, logo_url: e.target.value || null })
          }
          helperText={t("branding.logoUrlHelp")}
        />
        <Box sx={{ display: "flex", gap: 2 }}>
          <TextField
            label={t("branding.primaryColour")}
            size="small"
            value={form.primary_color ?? ""}
            onChange={(e) =>
              setForm({ ...form, primary_color: e.target.value || null })
            }
            helperText={t("branding.primaryColourHelp")}
            sx={{ flex: 1 }}
            InputProps={{
              endAdornment: form.primary_color ? (
                <Box
                  sx={{
                    width: 20,
                    height: 20,
                    borderRadius: 0.5,
                    bgcolor: form.primary_color,
                    border: 1,
                    borderColor: "divider",
                    ml: 1,
                  }}
                />
              ) : undefined,
            }}
          />
          <TextField
            label={t("branding.secondaryColour")}
            size="small"
            value={form.secondary_color ?? ""}
            onChange={(e) =>
              setForm({ ...form, secondary_color: e.target.value || null })
            }
            helperText={t("branding.secondaryColourHelp")}
            sx={{ flex: 1 }}
            InputProps={{
              endAdornment: form.secondary_color ? (
                <Box
                  sx={{
                    width: 20,
                    height: 20,
                    borderRadius: 0.5,
                    bgcolor: form.secondary_color,
                    border: 1,
                    borderColor: "divider",
                    ml: 1,
                  }}
                />
              ) : undefined,
            }}
          />
        </Box>
        <TextField
          label={t("branding.fontFamily")}
          size="small"
          value={form.font_family ?? ""}
          onChange={(e) =>
            setForm({ ...form, font_family: e.target.value || null })
          }
          helperText={t("branding.fontFamilyHelp")}
        />
      </Box>

      {saveMut.isError && (
        <Alert severity="error" sx={{ mt: 2 }}>
          {(saveMut.error as { response?: { data?: { detail?: string } } })
            ?.response?.data?.detail ?? t("branding.saveFailed")}
        </Alert>
      )}
      {saveMut.isSuccess && (
        <Alert severity="success" sx={{ mt: 2 }}>
          {t("branding.saved")}
        </Alert>
      )}

      <Box sx={{ mt: 3 }}>
        <Button
          variant="contained"
          startIcon={
            saveMut.isPending ? <CircularProgress size={14} /> : <SaveIcon />
          }
          onClick={() => saveMut.mutate()}
          disabled={saveMut.isPending}
        >
          {t("branding.save")}
        </Button>
      </Box>
    </Box>
  );
}
