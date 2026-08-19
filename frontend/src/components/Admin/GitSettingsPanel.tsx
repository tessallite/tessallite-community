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
import api from "../../api/client";

type GitSettings = {
  remote_url: string | null;
  has_token: boolean;
};

type GitTestResult = {
  success: boolean;
  message: string;
};

const gitSettingsApi = {
  get: () =>
    api
      .get<GitSettings>("/api/v1/tenants/me/git-settings")
      .then((r) => r.data),
  save: (data: { remote_url: string; token?: string | null }) =>
    api
      .put<GitSettings>("/api/v1/tenants/me/git-settings", data)
      .then((r) => r.data),
  test: () =>
    api
      .post<GitTestResult>("/api/v1/tenants/me/git-settings/test")
      .then((r) => r.data),
};

export default function GitSettingsPanel() {
  const t = useT();
  const qc = useQueryClient();
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [dirty, setDirty] = useState(false);
  const [testResult, setTestResult] = useState<GitTestResult | null>(null);

  const settingsQ = useQuery({
    queryKey: ["git-settings"],
    queryFn: gitSettingsApi.get,
  });

  useEffect(() => {
    if (settingsQ.data && !dirty) {
      setUrl(settingsQ.data.remote_url ?? "");
    }
  }, [settingsQ.data, dirty]);

  const saveMut = useMutation({
    mutationFn: () =>
      gitSettingsApi.save({
        remote_url: url,
        token: token || undefined,
      }),
    onSuccess: () => {
      setDirty(false);
      setToken("");
      qc.invalidateQueries({ queryKey: ["git-settings"] });
    },
  });

  const testMut = useMutation({
    mutationFn: gitSettingsApi.test,
    onSuccess: (data) => setTestResult(data),
  });

  if (settingsQ.isLoading) {
    return (
      <Box sx={{ p: 4, textAlign: "center" }}>
        <CircularProgress size={24} />
      </Box>
    );
  }

  return (
    <Box sx={{ maxWidth: 600, py: 2 }}>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2 }}>
        <Typography variant="h6">{t("git.sectionTitle")}</Typography>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        {t("git.sectionDescription")}
      </Typography>

      <Stack spacing={2}>
        <TextField
          label={t("git.remoteUrl")}
          placeholder={t("git.remoteUrlPlaceholder")}
          value={url}
          onChange={(e) => {
            setUrl(e.target.value);
            setDirty(true);
          }}
          fullWidth
          size="small"
        />
        <TextField
          label={t("git.token")}
          placeholder={t("git.tokenPlaceholder")}
          value={token}
          onChange={(e) => {
            setToken(e.target.value);
            setDirty(true);
          }}
          type="password"
          fullWidth
          size="small"
          helperText={
            settingsQ.data?.has_token && !token
              ? t("git.tokenSaved")
              : undefined
          }
        />

        <Stack direction="row" spacing={1}>
          <Button
            variant="contained"
            size="small"
            onClick={() => saveMut.mutate()}
            disabled={!dirty || saveMut.isPending}
          >
            {saveMut.isPending ? <CircularProgress size={16} /> : t("git.save")}
          </Button>
          <Button
            variant="outlined"
            size="small"
            onClick={() => {
              setTestResult(null);
              testMut.mutate();
            }}
            disabled={testMut.isPending || !settingsQ.data?.remote_url}
          >
            {testMut.isPending ? (
              <CircularProgress size={16} />
            ) : (
              t("git.testConnection")
            )}
          </Button>
        </Stack>

        {testResult && (
          <Alert severity={testResult.success ? "success" : "error"}>
            {testResult.success
              ? t("git.testSuccess")
              : t("git.testFailed", { error: testResult.message })}
          </Alert>
        )}

        {saveMut.isError && (
          <Alert severity="error">
            Save failed.
          </Alert>
        )}
      </Stack>
    </Box>
  );
}
