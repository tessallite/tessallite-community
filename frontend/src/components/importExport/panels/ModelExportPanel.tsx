import {
  Alert,
  CircularProgress,
  List,
  ListItem,
  ListItemButton,
  ListItemText,
  Stack,
  Typography,
} from "@mui/material";
import { useMutation, useQuery } from "@tanstack/react-query";
import { modelsApi } from "../../../api/client";
import { importExportApi } from "../../../api/importExportApi";
import { useT } from "../../../i18n";

type Props = {
  projectId: string;
  onDone: () => void;
};

export default function ModelExportPanel({ projectId, onDone }: Props) {
  const t = useT();
  const { data: models, isLoading } = useQuery({
    queryKey: ["models", projectId],
    queryFn: () => modelsApi.list(projectId),
  });

  const exportMut = useMutation({
    mutationFn: (modelId: string) =>
      importExportApi.exportModel(projectId, modelId),
    onSuccess: (data) => {
      // Bug-6292: persist the authoritative connection stub list alongside the
      // bundle so the import dialog rebinds against the real connection_type
      // vocabulary instead of re-deriving it (lossily) from the snapshot's
      // source_type/target_type.
      const fileContent = {
        ...data.bundle,
        connections_required: data.connections_required,
      };
      const blob = new Blob([JSON.stringify(fileContent, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${data.bundle.model_slug || "model"}.tessallite.json`;
      a.click();
      URL.revokeObjectURL(url);
      onDone();
    },
  });

  return (
    <Stack spacing={2}>
      <Typography variant="body2" color="text.secondary">
        {t("exportDialog.modelDescription")}
      </Typography>
      {isLoading && <CircularProgress size={20} />}
      {models && models.length === 0 && (
        <Alert severity="info">{t("exportDialog.noModels")}</Alert>
      )}
      {models && models.length > 0 && (
        <List dense disablePadding>
          {models.map((m) => (
            <ListItem key={m.id} disableGutters disablePadding>
              <ListItemButton
                onClick={() => exportMut.mutate(m.id)}
                disabled={exportMut.isPending}
              >
                <ListItemText
                  primary={m.display_name}
                  secondary={m.slug}
                />
              </ListItemButton>
            </ListItem>
          ))}
        </List>
      )}
      {exportMut.isError && (
        <Alert severity="error">
          {(exportMut.error as Error)?.message || t("exportDialog.exportFailed")}
        </Alert>
      )}
    </Stack>
  );
}
