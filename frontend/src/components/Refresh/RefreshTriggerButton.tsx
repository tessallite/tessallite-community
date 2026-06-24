import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button, CircularProgress } from "@mui/material";
import RefreshIcon from "@mui/icons-material/Refresh";
import { pocketsApi, schedulerApiClient } from "../../api/client";
import { useT } from "../../i18n";

type EntityType = "aggregate" | "pocket";

interface Props {
  entityId: string;
  modelId: string;
  projectId: string;
  entityType: EntityType;
  mode?: "full" | "incremental";
  label?: string;
  size?: "small" | "medium";
  variant?: "text" | "outlined" | "contained";
  onResult?: (ok: boolean, message: string) => void;
}

export default function RefreshTriggerButton({
  entityId,
  modelId,
  projectId,
  entityType,
  mode = "full",
  label,
  size = "small",
  variant = "outlined",
  onResult,
}: Props) {
  const t = useT();
  const qc = useQueryClient();
  const [done, setDone] = useState(false);

  const mutation = useMutation({
    mutationFn: async (): Promise<{ status: string; error_message?: string | null }> => {
      if (entityType === "pocket") {
        return pocketsApi.refresh(projectId, modelId, entityId);
      }
      return schedulerApiClient.triggerRefresh({
        aggregate_id: entityId,
        model_id: modelId,
        mode,
      });
    },
    onSuccess: (data) => {
      const ok = data?.status !== "failed";
      // F-005-23: pocket refresh is asynchronous — the endpoint returns 202 with
      // a "queued" run rather than a completed one. Report "started" (not
      // "completed") for the still-in-flight states so the message is accurate,
      // and invalidate the pocket queries so the row's status (queued →
      // invalidating → fresh) and run history refresh as the job progresses.
      const inFlight =
        data?.status === "queued" ||
        data?.status === "running" ||
        data?.status === "invalidating";
      const message = !ok
        ? data?.error_message ?? t("refreshButton.rebuildFailed")
        : inFlight
          ? t("refreshButton.rebuildStarted")
          : t("refreshButton.rebuildCompleted");
      onResult?.(ok, message);
      setDone(true);
      setTimeout(() => setDone(false), 2000);
      qc.invalidateQueries({ queryKey: ["aggregates", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["aggregate-runs"] });
      qc.invalidateQueries({ queryKey: ["runs"] });
      qc.invalidateQueries({ queryKey: ["pockets", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["pocket-runs"] });
    },
    onError: () => {
      onResult?.(false, t("refreshButton.requestFailed"));
    },
  });

  const defaultLabel =
    mode === "incremental" ? t("refreshButton.onlyNewRows") : t("refreshButton.rebuildNow");
  const displayLabel = label ?? defaultLabel;

  return (
    <Button
      size={size}
      variant={variant}
      startIcon={
        mutation.isPending ? (
          <CircularProgress size={14} color="inherit" />
        ) : (
          <RefreshIcon sx={{ fontSize: 16 }} />
        )
      }
      onClick={() => mutation.mutate()}
      disabled={mutation.isPending}
      color={done ? "success" : "primary"}
      data-testid={`refresh-trigger-${mode}`}
    >
      {mutation.isPending ? t("refreshButton.rebuilding") : displayLabel}
    </Button>
  );
}
