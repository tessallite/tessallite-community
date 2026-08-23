import { useMemo, useState } from "react";
import { useT } from "../../i18n";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tabs,
  Typography,
} from "@mui/material";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import {
  parseJoinPopulationBlockedError,
  versionsApi,
  type JoinPopulationBlockedDetail,
} from "../../api/versionsApi";
import JoinPopulationBlockedNotice from "../Deploy/JoinPopulationBlockedNotice";
import { useModel } from "../../api/hooks";
import { isTenantAdmin } from "../../auth/currentUser";
import { useConfirm } from "../Confirm";
import { useModelEditorStore } from "../../store/useModelEditorStore";
import VersionDiffPanel from "./VersionDiffPanel";
import PendingChangesPanel from "./PendingChangesPanel";
import GitTimeline from "./GitTimeline";
import { invalidateModelScopedCaches } from "./modelCacheInvalidation";

// Bug-7616: surface the backend error instead of swallowing it. Mirrors the
// panel-level extractError pattern used across the Panels dialogs.
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
  open: boolean;
  onClose: () => void;
  projectId: string;
  modelId: string;
  onOpenJoins?: () => void;
};

/**
 * Lists every saved version for the model, with Deploy + Revert actions.
 * Deploy points the runtime at the chosen version (modeler+). Revert appends a
 * new version equal to the chosen one's definition and makes it current
 * (Bug-7906: no version is deleted; history is preserved and the revert is
 * itself revertible); typed-name confirm, admin-only (Bug-7616), matching
 * versions.py require_role("admin"). Deploy/Revert failures surface an inline
 * error instead of failing silently.
 */
export default function VersionsDialog({
  open,
  onClose,
  projectId,
  modelId,
  onOpenJoins,
}: Props) {
  const t = useT();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const markClean = useModelEditorStore((s) => s.markClean);
  const [activeTab, setActiveTab] = useState<
    "history" | "pending" | "diff" | "timeline"
  >("history");
  // Bug-7616: a Deploy/Revert failure must be shown, not swallowed.
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionRefusal, setActionRefusal] =
    useState<JoinPopulationBlockedDetail | null>(null);
  // F-013-04: the backend returns a governance-preserved note on revert
  // (personas / RLS / data-tags are NOT rolled back). It was typed but never
  // rendered, so an admin believed security had been reverted. Surface it.
  const [revertNote, setRevertNote] = useState<string | null>(null);
  // Revert requires project `admin` on the backend (versions.py
  // require_role("admin")). G-013-02: that gate admits a PROJECT-scoped admin
  // binding, not only a tenant/system admin, so gating on isTenantAdmin() alone
  // hid the button from a project admin who CAN revert via the API. Use the
  // server-derived caller_can_admin (same binding precedence as the route),
  // falling back to isTenantAdmin() for list/absent responses. Backend stays
  // authoritative.
  const model = useModel(projectId, modelId);
  const canRevert = isTenantAdmin() || model.data?.caller_can_admin === true;

  const versions = useQuery({
    queryKey: ["versions", projectId, modelId],
    queryFn: () => versionsApi.list(projectId, modelId),
    enabled: open,
  });

  const deployMut = useMutation({
    mutationFn: (versionId: string) =>
      versionsApi.deploy(projectId, modelId, versionId),
    onSuccess: (data) => {
      setActionError(null);
      setActionRefusal(null);
      markClean({
        deployedVersion:
          versions.data?.find((v) => v.id === data.deployed_version_id)
            ?.version_number ?? null,
        lastDeployedAt: data.last_deployed_at,
      });
      qc.invalidateQueries({ queryKey: ["versions", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["models", projectId, modelId] });
    },
    onError: (e: unknown) => {
      const refusal = parseJoinPopulationBlockedError(e);
      if (refusal) {
        setActionError(null);
        setActionRefusal(refusal);
        return;
      }
      setActionRefusal(null);
      setActionError(
        t("versions.deployFailed", {
          error: extractError(e) || t("errors.requestFailed"),
        }),
      );
    },
  });

  const revertMut = useMutation({
    mutationFn: (args: { versionId: string; confirm: string }) =>
      versionsApi.revert(projectId, modelId, args.versionId, args.confirm),
    onSuccess: (data) => {
      setActionError(null);
      // F-013-04: show what revert did and did NOT roll back (governance is
      // preserved). The server note is already locale-neutral English.
      setRevertNote(data?.governance_preserved_note || null);
      // Revert is a hard rewrite of live state — invalidate every model-scoped
      // cache (single source of truth shared with the pending-change discard
      // actions, so the two paths cannot drift). Bug-5656 / Bug-7152 keys are
      // included in that list.
      invalidateModelScopedCaches(qc, projectId, modelId);
      markClean();
    },
    onError: (e: unknown) =>
      setActionError(
        t("versions.revertFailed", {
          error: extractError(e) || t("errors.requestFailed"),
        }),
      ),
  });

  async function handleDeploy(versionId: string, n: number) {
    const ok = await confirm({
      title: t("versions.deployTitle"),
      message: t("versions.deployMessage", { n: String(n) }),
      confirmLabel: t("versions.deployConfirm", { n: String(n) }),
      destructive: false,
    });
    if (ok) deployMut.mutate(versionId);
  }

  async function handleRevert(versionId: string, n: number) {
    // Bug-6201 / F-013-01: the localized `versions.revertConfirmText` phrase is
    // typed-name UI friction ONLY — ConfirmDialog validates the typed text on
    // the client. The value SENT to the backend must be the language-neutral
    // version ID, which versions.py accepts alongside the legacy English
    // "revert to v{N}" phrase. The backend does NOT know any translated phrase,
    // so sending `confirmPhrase` 400s on every non-English locale (7 of 8
    // shipped). Sending `versionId` keeps revert usable in every locale.
    const confirmPhrase = t("versions.revertConfirmText", { n: String(n) });
    const ok = await confirm({
      mode: "typed-name",
      title: t("versions.revertTitle", { n: String(n) }),
      message: (
        <span>
          {t("versions.revertMessage", { n: String(n) })}
        </span>
      ),
      confirmText: confirmPhrase,
      confirmLabel: t("versions.revertConfirm", { n: String(n) }),
    });
    if (ok) revertMut.mutate({ versionId, confirm: versionId });
  }

  // The history table: sort so the version the gateway is currently serving
  // is always first, then the rest by version_number descending. Deploy can
  // point the runtime at ANY existing version (not only the newest), and the
  // list itself is otherwise plain version_number-desc — without this, an
  // older deployed version can be scrolled below newer, undeployed drafts,
  // making "currently serving" easy to miss. Scoped to the history table only
  // — the Diff tab keeps plain version_number order for its A/B defaults.
  const sortedVersions = useMemo(() => {
    const list = versions.data ?? [];
    return [...list].sort((a, b) => {
      if (a.is_deployed !== b.is_deployed) return a.is_deployed ? -1 : 1;
      return b.version_number - a.version_number;
    });
  }, [versions.data]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>{t("versions.title")}</DialogTitle>
      <Box sx={{ borderBottom: 1, borderColor: "divider", px: 3 }}>
        <Tabs
          value={activeTab}
          onChange={(_, v) => setActiveTab(v)}
          textColor="primary"
          indicatorColor="primary"
        >
          <Tab label={t("versions.historyTab")} value="history" />
          <Tab label={t("versions.pendingTab")} value="pending" />
          <Tab label={t("versions.diffTab")} value="diff" disabled={!versions.data || versions.data.length < 2} />
          <Tab label={t("versions.timelineTab")} value="timeline" />
        </Tabs>
      </Box>
      <DialogContent>
        {actionError && (
          <Alert severity="error" sx={{ mb: 2 }} onClose={() => setActionError(null)}>
            {actionError}
          </Alert>
        )}
        {actionRefusal && (
          <JoinPopulationBlockedNotice
            detail={actionRefusal}
            onClose={() => setActionRefusal(null)}
            onOpenJoins={onOpenJoins}
          />
        )}
        {revertNote && (
          <Alert severity="success" sx={{ mb: 2 }} onClose={() => setRevertNote(null)}>
            {revertNote}
          </Alert>
        )}
        {activeTab === "history" && versions.isLoading && (
          <Box sx={{ p: 4, textAlign: "center" }}>
            <CircularProgress size={24} />
          </Box>
        )}
        {activeTab === "history" && versions.data && versions.data.length === 0 && (
          <Typography color="text.secondary" sx={{ py: 4, textAlign: "center" }}>
            {t("versions.noVersions")}
          </Typography>
        )}
        {activeTab === "pending" && (
          <PendingChangesPanel
            projectId={projectId}
            modelId={modelId}
            canRevert={canRevert}
          />
        )}
        {activeTab === "diff" && versions.data && (
          <VersionDiffPanel
            projectId={projectId}
            modelId={modelId}
            versions={versions.data}
          />
        )}
        {activeTab === "timeline" && (
          <GitTimeline projectId={projectId} modelId={modelId} />
        )}
        {activeTab === "history" && sortedVersions.length > 0 && (
          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>{t("versions.version")}</TableCell>
                  <TableCell>{t("versions.created")}</TableCell>
                  <TableCell>{t("versions.by")}</TableCell>
                  <TableCell>{t("versions.summary")}</TableCell>
                  <TableCell>{t("versions.deployed")}</TableCell>
                  <TableCell align="right">{t("versions.actions")}</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {sortedVersions.map((v) => (
                  <TableRow key={v.id} hover>
                    <TableCell>
                      <strong>v{v.version_number}</strong>
                    </TableCell>
                    <TableCell>{new Date(v.created_at).toLocaleString()}</TableCell>
                    <TableCell>
                      <Typography variant="body2" sx={{ fontFamily: "monospace" }}>
                        {v.created_by}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2" color="text.secondary">
                          {v.summary || t("common.na")}
                      </Typography>
                    </TableCell>
                    <TableCell>
                      {/* Bug-7906: a revert leaves NEWER versions in this
                          table while an older one is live, and "Deployed"
                          reads as "was deployed at some point". Name what the
                          gateway is serving right now instead. The star is the
                          favourite mark elsewhere in the product. */}
                      {v.is_deployed && (
                        <Chip
                          icon={<CheckCircleIcon />}
                          label={t("versions.currentlyServing")}
                          color="success"
                          size="small"
                        />
                      )}
                    </TableCell>
                    <TableCell align="right">
                      <Stack direction="row" spacing={1} justifyContent="flex-end">
                        {/* Bug-6295: an imported placeholder version has no
                            usable snapshot. It cannot be deployed or reverted to
                            (the backend rejects both), so offer neither action
                            and label it instead. */}
                        {v.snapshot_unavailable ? (
                          <Typography variant="caption" color="text.secondary">
                            {t("versions.snapshotUnavailable")}
                          </Typography>
                        ) : (
                          <>
                            {!v.is_deployed && (
                              <Button
                                size="small"
                                variant="outlined"
                                onClick={() => handleDeploy(v.id, v.version_number)}
                                disabled={deployMut.isPending}
                              >
                                {t("versions.deployButton")}
                              </Button>
                            )}
                            {canRevert && (
                              <Button
                                size="small"
                                variant="outlined"
                                onClick={() => handleRevert(v.id, v.version_number)}
                                disabled={revertMut.isPending}
                              >
                                {t("versions.revertButton")}
                              </Button>
                            )}
                          </>
                        )}
                      </Stack>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t("versions.close")}</Button>
      </DialogActions>
    </Dialog>
  );
}
