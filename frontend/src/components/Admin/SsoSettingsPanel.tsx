import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { useT } from "../../i18n";
import { ssoApi } from "../../api/client";
import HelpIconButton from "../HelpIconButton";

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export default function SsoSettingsPanel() {
  const t = useT();
  const qc = useQueryClient();
  const [issuer, setIssuer] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [metadataUrl, setMetadataUrl] = useState("");
  const [dirty, setDirty] = useState(false);

  const configQ = useQuery({
    queryKey: ["sso-config"],
    queryFn: ssoApi.getConfig,
  });
  const backendsQ = useQuery({
    queryKey: ["sso-backends"],
    queryFn: () => ssoApi.getBackends(),
  });

  useEffect(() => {
    if (configQ.data && !dirty) {
      setIssuer(asString(configQ.data.oidc?.issuer));
      setClientId(asString(configQ.data.oidc?.client_id));
      setMetadataUrl(asString(configQ.data.saml?.idp_metadata_url));
      setClientSecret("");
    }
  }, [configQ.data, dirty]);

  const secretSet = Boolean(configQ.data?.oidc?.client_secret_set);

  const saveMut = useMutation({
    mutationFn: () => {
      const oidc: Record<string, unknown> = {
        issuer,
        client_id: clientId,
      };
      if (clientSecret) {
        oidc.client_secret = clientSecret;
      }
      return ssoApi.putConfig({
        oidc,
        saml: { idp_metadata_url: metadataUrl },
      });
    },
    onSuccess: () => {
      setDirty(false);
      setClientSecret("");
      qc.invalidateQueries({ queryKey: ["sso-config"] });
    },
  });

  if (configQ.isLoading) {
    return (
      <Box sx={{ p: 4, textAlign: "center" }}>
        <CircularProgress size={24} />
      </Box>
    );
  }

  return (
    <Box sx={{ maxWidth: 640, py: 2 }}>
      <Typography variant="h6" sx={{ mb: 1 }}>
        {t("sso.panelTitle")}
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        {t("sso.panelHelp")}
      </Typography>

      {configQ.isError && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {t("sso.loadFailed")}
        </Alert>
      )}

      <Stack spacing={2}>
        <TextField
          label={t("sso.oidcIssuer")}
          value={issuer}
          onChange={(e) => {
            setIssuer(e.target.value);
            setDirty(true);
          }}
          fullWidth
          size="small"
        />
        <TextField
          label={t("sso.oidcClientId")}
          value={clientId}
          onChange={(e) => {
            setClientId(e.target.value);
            setDirty(true);
          }}
          fullWidth
          size="small"
        />
        <TextField
          label={t("sso.oidcClientSecret")}
          value={clientSecret}
          onChange={(e) => {
            setClientSecret(e.target.value);
            setDirty(true);
          }}
          type="password"
          autoComplete="new-password"
          fullWidth
          size="small"
          helperText={secretSet ? t("sso.secretAlreadySet") : t("sso.secretHint")}
        />
        <TextField
          label={t("sso.samlMetadataUrl")}
          value={metadataUrl}
          onChange={(e) => {
            setMetadataUrl(e.target.value);
            setDirty(true);
          }}
          fullWidth
          size="small"
        />

        <Alert
          severity="info"
          // Bug-9308: honest-label variant — the status line already states
          // GCP IAM config is env-only (sso.gcpIamOn/Off); a help link
          // clarifying that env-only configuration is the only thing this
          // panel was missing (no audience/domain fields are added — that
          // would need a backend config API, out of this lane's scope).
          action={<HelpIconButton href="/help/admin/sso-configuration.html" />}
        >
          {backendsQ.data?.gcp_iam_enabled ? t("sso.gcpIamOn") : t("sso.gcpIamOff")}
        </Alert>
        <Alert severity="info">
          {backendsQ.data?.ldap_enabled ? t("sso.ldapOn") : t("sso.ldapOff")}
        </Alert>

        <Button
          variant="contained"
          size="small"
          onClick={() => saveMut.mutate()}
          disabled={!dirty || saveMut.isPending}
          sx={{ alignSelf: "flex-start" }}
        >
          {saveMut.isPending ? <CircularProgress size={16} /> : t("sso.save")}
        </Button>

        {saveMut.isSuccess && (
          <Alert severity="success">{t("sso.saved")}</Alert>
        )}
        {saveMut.isError && (
          <Alert severity="error">{t("sso.saveFailed")}</Alert>
        )}
      </Stack>
    </Box>
  );
}
