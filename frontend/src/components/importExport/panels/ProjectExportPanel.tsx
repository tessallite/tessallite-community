import { useState } from "react";
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
import { useMutation } from "@tanstack/react-query";
import {
  type ProjectExportRequest,
  projectImportExportApi,
} from "../../../api/importExportApi";
import { useT } from "../../../i18n";
import { SECTION_KEYS } from "../helpers";

type Props = {
  projectId: string;
  projectSlug: string;
  onDone: () => void;
};

export default function ProjectExportPanel({
  projectId,
  projectSlug,
  onDone,
}: Props) {
  const t = useT();
  const [sections, setSections] = useState<Set<string>>(
    new Set(SECTION_KEYS.filter((s) => s.defaultOn).map((s) => s.key)),
  );
  const [includeCreds, setIncludeCreds] = useState(false);
  const [credAck, setCredAck] = useState(false);
  const [passphrase, setPassphrase] = useState("");
  const [confirmPass, setConfirmPass] = useState("");

  const toggleSection = (key: string) => {
    setSections((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const exportMut = useMutation({
    mutationFn: async () => {
      const body: ProjectExportRequest = {
        include_credentials: includeCreds,
        passphrase: includeCreds ? passphrase : null,
        sections: Array.from(sections),
      };
      return projectImportExportApi.exportProject(projectId, body);
    },
    onSuccess: (data) => {
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const date = new Date().toISOString().slice(0, 10);
      a.download = `${projectSlug}-export-${date}.json`;
      a.click();
      URL.revokeObjectURL(url);
      onDone();
    },
  });

  const passValid =
    !includeCreds || (passphrase.length >= 8 && passphrase === confirmPass);
  const credAckValid = !includeCreds || credAck;
  const canSubmit = passValid && credAckValid && !exportMut.isPending;

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t("exportDialog.projectDescription")}
      </Typography>

      <Typography variant="subtitle2">
        {t("projectImportExport.includedInExport")}
      </Typography>
      {SECTION_KEYS.map((opt) => (
        <FormControlLabel
          key={opt.key}
          control={
            <Checkbox
              checked={sections.has(opt.key)}
              onChange={() => toggleSection(opt.key)}
            />
          }
          label={t(opt.i18nKey)}
        />
      ))}

      <FormControlLabel
        control={
          <Checkbox
            checked={includeCreds}
            onChange={(_, v) => {
              setIncludeCreds(v);
              if (!v) setCredAck(false);
            }}
          />
        }
        label={t("exportDialog.includeCredentials")}
      />

      {includeCreds && (
        <Box>
          <Alert severity="warning" sx={{ mb: 2 }}>
            {t("exportDialog.credentialsWarning")}
          </Alert>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            {t("exportDialog.credentialsEncryptionNote")}
          </Typography>
          <TextField
            label={t("exportDialog.passphraseLabel")}
            type="password"
            fullWidth
            value={passphrase}
            onChange={(e) => setPassphrase(e.target.value)}
            helperText={t("exportDialog.passphraseHelp")}
            sx={{ mb: 1 }}
          />
          <TextField
            label={t("exportDialog.confirmPassphrase")}
            type="password"
            fullWidth
            value={confirmPass}
            onChange={(e) => setConfirmPass(e.target.value)}
            error={confirmPass.length > 0 && passphrase !== confirmPass}
            helperText={
              confirmPass.length > 0 && passphrase !== confirmPass
                ? t("exportDialog.passphraseMismatch")
                : ""
            }
            sx={{ mb: 1 }}
          />
          <FormControlLabel
            control={
              <Checkbox
                checked={credAck}
                onChange={(_, v) => setCredAck(v)}
              />
            }
            label={t("exportDialog.credentialAck")}
          />
        </Box>
      )}

      {exportMut.isError && (
        <Alert severity="error">
          {(exportMut.error as Error)?.message || t("exportDialog.exportFailed")}
        </Alert>
      )}

      <Box display="flex" justifyContent="flex-end" pt={1}>
        <Button
          variant="contained"
          disabled={!canSubmit}
          onClick={() => exportMut.mutate()}
        >
          {exportMut.isPending ? (
            <CircularProgress size={18} color="inherit" />
          ) : (
            t("exportDialog.exportButton")
          )}
        </Button>
      </Box>
    </Stack>
  );
}
