import { useState } from "react";
import { useT } from "../../i18n";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  Stack,
  Typography,
} from "@mui/material";
import UndoIcon from "@mui/icons-material/Undo";
import RestartAltIcon from "@mui/icons-material/RestartAlt";
import {
  countDiffChanges,
  usePendingChanges,
  versionsApi,
} from "../../api/versionsApi";
import type { DiffMap } from "../../api/versionsApi";
import { useConfirm } from "../Confirm";
import { useModelEditorStore } from "../../store/useModelEditorStore";
import { DiffBody, useDiffCategoryLabels } from "./VersionDiffPanel";
import { invalidateModelScopedCaches } from "./modelCacheInvalidation";

// Mirrors the panel-level extractError pattern used across the builder dialogs
// (Bug-7616): surface the backend detail instead of swallowing it.
function extractError(e: unknown): string {
  const err = e as {
    response?: { data?: { detail?: string | { message?: string } } };
    message?: string;
  };
  const detail = err?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return err?.message ?? "";
}

type Props = {
  projectId: string;
  modelId: string;
  /** Whether the caller may revert (project/tenant admin). Gates discard-to-
   *  deployed, which is the admin Revert operation under the hood. */
  canRevert: boolean;
};

/**
 * PendingChangesPanel — G-013-01 (Bug-9171). Surfaces the two pending change
 * sets a modeller must be able to SEE and ACT on:
 *
 *   - Unsaved edits: the in-editor draft vs the last SAVED version.
 *   - Saved but not deployed: the last saved version vs the DEPLOYED snapshot.
 *
 * Each set is reviewable inline (the same diff renderer the version A/B diff
 * uses) and discardable:
 *
 *   - "Discard unsaved edits" resets the draft to the last saved version
 *     (POST /discard-draft). Draft-only: it never changes what the gateway
 *     serves, so any modeller can do it.
 *   - "Discard to deployed version" restores the draft all the way back to the
 *     deployed version, discarding unsaved AND saved-but-undeployed draft
 *     changes. It reuses the admin Revert endpoint (history is preserved), so
 *     it is gated on {@link Props.canRevert}.
 */
export default function PendingChangesPanel({ projectId, modelId, canRevert }: Props) {
  const t = useT();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const labels = useDiffCategoryLabels();
  const markClean = useModelEditorStore((s) => s.markClean);

  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [showUnsaved, setShowUnsaved] = useState(false);
  const [showSaved, setShowSaved] = useState(false);

  const pending = usePendingChanges(projectId, modelId, true);
  // Reuse the versions list (same query key -> deduped) to resolve the deployed
  // version's ID for the revert-backed discard-to-deployed action.
  const versions = useQuery({
    queryKey: ["versions", projectId, modelId],
    queryFn: () => versionsApi.list(projectId, modelId),
  });
  const deployedRow = versions.data?.find((v) => v.is_deployed) ?? null;

  const discardMut = useMutation({
    mutationFn: () => versionsApi.discardDraft(projectId, modelId),
    onSuccess: () => {
      setError(null);
      setNote(null);
      invalidateModelScopedCaches(qc, projectId, modelId);
      // Live state now equals the last saved version: no unsaved edits remain
      // and the save/deploy pointers did not move.
      markClean();
    },
    onError: (e: unknown) =>
      setError(
        t("pendingChanges.discardFailed", {
          error: extractError(e) || t("errors.requestFailed"),
        }),
      ),
  });

  const revertMut = useMutation({
    // discard-to-deployed reuses Revert: the backend accepts the version ID as
    // the language-neutral confirmation token (VersionsDialog does the same).
    mutationFn: (versionId: string) =>
      versionsApi.revert(projectId, modelId, versionId, versionId),
    onSuccess: (data) => {
      setError(null);
      setNote(data?.governance_preserved_note || null);
      invalidateModelScopedCaches(qc, projectId, modelId);
      // Revert appended a new head equal to the deployed definition and moved
      // the pointer to it; the exact new numbers arrive when ModelBuilder
      // refetches the invalidated models/versions queries. Clear dirty now so
      // the banner does not lag.
      markClean();
    },
    onError: (e: unknown) =>
      setError(
        t("pendingChanges.discardFailed", {
          error: extractError(e) || t("errors.requestFailed"),
        }),
      ),
  });

  async function handleDiscardUnsaved(baseVersion: number) {
    const ok = await confirm({
      title: t("pendingChanges.discardUnsavedTitle"),
      message: t("pendingChanges.discardUnsavedMessage", { n: String(baseVersion) }),
      confirmLabel: t("pendingChanges.discardUnsavedConfirm"),
      destructive: true,
    });
    if (ok) discardMut.mutate();
  }

  async function handleDiscardToDeployed(deployedVersion: number, versionId: string) {
    const ok = await confirm({
      mode: "typed-name",
      title: t("pendingChanges.discardToDeployedTitle", { n: String(deployedVersion) }),
      message: (
        <span>{t("pendingChanges.discardToDeployedMessage", { n: String(deployedVersion) })}</span>
      ),
      confirmText: t("pendingChanges.discardToDeployedConfirmText"),
      confirmLabel: t("pendingChanges.discardToDeployedConfirm"),
    });
    if (ok) revertMut.mutate(versionId);
  }

  if (pending.isLoading) {
    return (
      <Box sx={{ p: 4, textAlign: "center" }}>
        <CircularProgress size={24} />
      </Box>
    );
  }
  if (pending.isError || !pending.data) {
    return (
      <Typography variant="body2" color="error.main" sx={{ py: 2 }}>
        {t("pendingChanges.loadFailed")}
      </Typography>
    );
  }

  const { unsaved, saved_undeployed } = pending.data;
  const unsavedCount = countDiffChanges(unsaved.diff as DiffMap);
  const savedCount = countDiffChanges(saved_undeployed.diff as DiffMap);
  const busy = discardMut.isPending || revertMut.isPending;
  const nothingPending =
    unsavedCount === 0 && savedCount === 0 && !unsaved.base_version_unavailable;

  return (
    <Box>
      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}
      {note && (
        <Alert severity="success" sx={{ mb: 2 }} onClose={() => setNote(null)}>
          {note}
        </Alert>
      )}

      {nothingPending && (
        <Typography color="text.secondary" sx={{ py: 3, textAlign: "center" }}>
          {t("pendingChanges.noPending")}
        </Typography>
      )}

      {/* -------- Unsaved edits: draft vs last saved -------- */}
      <Box mb={3}>
        <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap">
          <Typography variant="subtitle2" fontWeight={700}>
            {t("pendingChanges.unsavedTitle")}
          </Typography>
          {unsaved.base_version != null && !unsaved.base_version_unavailable && (
            <Typography variant="caption" color="text.secondary">
              {t("pendingChanges.baseSaved", { n: String(unsaved.base_version) })}
            </Typography>
          )}
          {!unsaved.base_version_unavailable && (
            <Chip
              size="small"
              label={
                unsavedCount !== 1
                  ? t("versionDiff.changesPlural", { count: String(unsavedCount) })
                  : t("versionDiff.changes", { count: String(unsavedCount) })
              }
            />
          )}
        </Stack>

        {unsaved.base_version == null && !unsaved.base_version_unavailable && (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
            {t("pendingChanges.neverSaved")}
          </Typography>
        )}
        {unsaved.base_version_unavailable && (
          <Alert severity="warning" sx={{ mt: 1 }}>
            {t("pendingChanges.baseUnavailable")}
          </Alert>
        )}

        {!unsaved.base_version_unavailable && unsavedCount > 0 && (
          <Stack direction="row" spacing={1} sx={{ mt: 1 }} flexWrap="wrap">
            <Button size="small" onClick={() => setShowUnsaved((v) => !v)}>
              {showUnsaved
                ? t("pendingChanges.hideChanges")
                : t("pendingChanges.reviewChanges")}
            </Button>
            {unsaved.base_version != null && (
              <Button
                size="small"
                color="warning"
                variant="outlined"
                startIcon={<UndoIcon />}
                disabled={busy}
                onClick={() => handleDiscardUnsaved(unsaved.base_version as number)}
              >
                {t("pendingChanges.discardUnsaved")}
              </Button>
            )}
          </Stack>
        )}
        {showUnsaved && !unsaved.base_version_unavailable && (
          <Box sx={{ mt: 1 }}>
            <DiffBody diff={unsaved.diff as DiffMap} labels={labels} />
          </Box>
        )}
      </Box>

      <Divider sx={{ mb: 3 }} />

      {/* -------- Saved but not deployed: last saved vs deployed -------- */}
      <Box>
        <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap">
          <Typography variant="subtitle2" fontWeight={700}>
            {t("pendingChanges.savedUndeployedTitle")}
          </Typography>
          {saved_undeployed.deployed_version != null && (
            <Typography variant="caption" color="text.secondary">
              {t("pendingChanges.baseDeployed", {
                n: String(saved_undeployed.deployed_version),
              })}
            </Typography>
          )}
          <Chip
            size="small"
            label={
              savedCount !== 1
                ? t("versionDiff.changesPlural", { count: String(savedCount) })
                : t("versionDiff.changes", { count: String(savedCount) })
            }
          />
        </Stack>

        {saved_undeployed.deployed_version == null && (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
            {t("pendingChanges.nothingDeployed")}
          </Typography>
        )}

        {(savedCount > 0 || unsavedCount > 0) && (
          <Stack direction="row" spacing={1} sx={{ mt: 1 }} flexWrap="wrap">
            {savedCount > 0 && (
              <Button size="small" onClick={() => setShowSaved((v) => !v)}>
                {showSaved
                  ? t("pendingChanges.hideChanges")
                  : t("pendingChanges.reviewChanges")}
              </Button>
            )}
            {canRevert && deployedRow && (
              <Button
                size="small"
                color="warning"
                variant="outlined"
                startIcon={<RestartAltIcon />}
                disabled={busy}
                onClick={() =>
                  handleDiscardToDeployed(
                    saved_undeployed.deployed_version as number,
                    deployedRow.id,
                  )
                }
              >
                {t("pendingChanges.discardToDeployed")}
              </Button>
            )}
          </Stack>
        )}
        {canRevert && deployedRow && (savedCount > 0 || unsavedCount > 0) && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
            {t("pendingChanges.discardToDeployedHint")}
          </Typography>
        )}
        {showSaved && (
          <Box sx={{ mt: 1 }}>
            <DiffBody diff={saved_undeployed.diff as DiffMap} labels={labels} />
          </Box>
        )}
      </Box>
    </Box>
  );
}
