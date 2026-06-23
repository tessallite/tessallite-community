import { useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Stack,
  Typography,
} from "@mui/material";
import { useMutation } from "@tanstack/react-query";
import { yamlExportApi } from "../../../api/importExportApi";
import { useT } from "../../../i18n";

type Props = {
  projectId: string;
  projectSlug: string;
  onDone: () => void;
};

export default function YamlExportPanel({
  projectId,
  projectSlug,
  onDone,
}: Props) {
  const t = useT();
  const [done, setDone] = useState(false);

  const exportMut = useMutation({
    mutationFn: () => yamlExportApi.exportProject(projectId),
    onSuccess: (blob) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const date = new Date().toISOString().slice(0, 10);
      a.download = `${projectSlug}-export-${date}.zip`;
      a.click();
      URL.revokeObjectURL(url);
      setDone(true);
    },
  });

  if (done) {
    return (
      <Box>
        <Alert severity="success">
          {t("exportDialog.exportDownloaded")}
        </Alert>
        <Box display="flex" justifyContent="flex-end" pt={2}>
          <Button variant="outlined" onClick={onDone}>
            {t("exportDialog.doneButton")}
          </Button>
        </Box>
      </Box>
    );
  }

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t("exportDialog.yamlDescription")}
      </Typography>
      <Typography variant="body2" color="text.secondary">
        {t("exportDialog.yamlFileNote")}
      </Typography>

      {exportMut.isError && (
        <Alert severity="error">
          {(exportMut.error as Error)?.message || t("exportDialog.exportFailed")}
        </Alert>
      )}

      <Box display="flex" justifyContent="flex-end" pt={1}>
        <Button
          variant="contained"
          disabled={exportMut.isPending}
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
