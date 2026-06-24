import { useState } from "react";
import {
  Alert,
  CircularProgress,
  List,
  ListItem,
  ListItemButton,
  ListItemText,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import { useMutation, useQuery } from "@tanstack/react-query";
import { modelsApi } from "../../../api/client";
import { lookmlExportApi } from "../../../api/importExportApi";
import { useT } from "../../../i18n";

type Props = {
  projectId: string;
  onDone: () => void;
};

export default function LookMLExportPanel({ projectId, onDone }: Props) {
  const t = useT();
  const [connection, setConnection] = useState(t("exportDialog.lookerDefaultConnection"));
  const { data: models, isLoading } = useQuery({
    queryKey: ["models", projectId],
    queryFn: () => modelsApi.list(projectId),
  });

  const exportMut = useMutation({
    mutationFn: (model: { id: string; slug: string }) =>
      lookmlExportApi.exportModel(projectId, model.id, connection.trim()),
    onSuccess: (blob, model) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${model.slug}-lookml.zip`;
      a.click();
      URL.revokeObjectURL(url);
      onDone();
    },
  });

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t("exportDialog.lookmlDescription")}
      </Typography>
      <Alert severity="info">
        {t("exportDialog.lookmlInfo")}
      </Alert>
      <TextField
        label={t("exportDialog.lookerConnectionLabel")}
        value={connection}
        onChange={(event) => setConnection(event.target.value)}
        helperText={t("exportDialog.lookerConnectionHelp")}
        fullWidth
      />
      {isLoading && <CircularProgress size={20} />}
      {models && models.length === 0 && (
        <Alert severity="info">{t("exportDialog.noModels")}</Alert>
      )}
      {models && models.length > 0 && (
        <List dense disablePadding>
          {models.map((model) => (
            <ListItem key={model.id} disableGutters disablePadding>
              <ListItemButton
                onClick={() => exportMut.mutate(model)}
                disabled={!connection.trim() || exportMut.isPending}
              >
                <ListItemText
                  primary={model.display_name}
                  secondary={t("exportDialog.lookerDeployedOnly", { slug: model.slug })}
                />
              </ListItemButton>
            </ListItem>
          ))}
        </List>
      )}
      {exportMut.isError && (
        <Alert severity="error">
          {(exportMut.error as Error)?.message || t("exportDialog.lookmlExportFailed")}
        </Alert>
      )}
    </Stack>
  );
}
