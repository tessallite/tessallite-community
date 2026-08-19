import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button, CircularProgress } from "@mui/material";
import RefreshIcon from "@mui/icons-material/Refresh";
import { connectionsApi, tableAttributesApi } from "../../../api/client";
import type { ModelTable } from "../../../api/types";
import { useT } from "../../../i18n";

interface Props {
  projectId: string;
  modelId: string;
  table: ModelTable;
  connectionId: string | null;
  onDone?: () => void;
  variant?: "outlined" | "contained" | "text";
  size?: "small" | "medium";
  label?: string;
}

/**
 * Re-discover physical columns from the source connection and persist them
 * to this model table. Surfaces in two empty-state spots inside the
 * unified dialog (Columns tab and Attributes tab) so a user can recover from
 * the three "table with no physical columns" cases (Bug-106/107/108)
 * without leaving the dialog.
 */
export default function SyncColumnsButton({
  projectId,
  modelId,
  table,
  connectionId,
  onDone,
  variant = "outlined",
  size = "small",
  label,
}: Props) {
  const t = useT();
  const qc = useQueryClient();

  const defaultLabel = label ?? t("tableEditColumns.syncButton");

  const sync = useMutation({
    mutationFn: async () => {
      if (!connectionId) throw new Error(t("tableEditColumns.noConnection"));
      const parts = table.physical_name.split(".");
      const schema = parts[0] ?? "public";
      const tableName = parts.slice(1).join(".") || parts[0];
      const cols = await connectionsApi.discoverColumns(projectId, connectionId, schema, tableName);
      if (cols.length === 0) {
        throw new Error(t("tableEditColumns.noColumnsFound"));
      }
      await tableAttributesApi.syncColumns(
        projectId,
        modelId,
        table.id,
        cols.map((c) => ({
          column_name: c.column_name,
          data_type: c.data_type,
          is_nullable: c.is_nullable,
          is_primary_key: c.is_primary_key,
        })),
      );
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tableAttributes", projectId, modelId, table.id] });
      qc.invalidateQueries({ queryKey: ["userDefinedAttributes", projectId, modelId, table.id] });
      onDone?.();
    },
  });

  return (
    <Button
      variant={variant}
      size={size}
      startIcon={sync.isPending ? <CircularProgress size={14} /> : <RefreshIcon />}
      onClick={() => sync.mutate()}
      disabled={!connectionId || sync.isPending}
    >
      {sync.isError
        ? (sync.error as Error).message ?? t("tableEditColumns.syncFailed")
        : sync.isSuccess
        ? t("tableEditColumns.synced")
        : defaultLabel}
    </Button>
  );
}
