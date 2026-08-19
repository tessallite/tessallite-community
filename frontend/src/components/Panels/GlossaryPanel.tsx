/**
 * GlossaryPanel — modeller-curated business glossary.
 *
 * Phase 3 of the semantic-layer plan (docs/architecture/architecture_semantic-layer.md). The
 * panel lists all glossary entries grouped by status, lets the modeller
 * approve / edit / reject LLM-proposed entries with one click, and shows
 * provenance (LLM vs user vs LLM-approved) prominently. Editing creates
 * a new version row server-side; the audit trail is preserved.
 */
import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome";
import CheckIcon from "@mui/icons-material/Check";
import EditIcon from "@mui/icons-material/Edit";
import CloseIcon from "@mui/icons-material/Close";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";
import IosShareIcon from "@mui/icons-material/IosShare";
import AddIcon from "@mui/icons-material/Add";
import UploadFileIcon from "@mui/icons-material/UploadFile";
import LinkOffIcon from "@mui/icons-material/LinkOff";
import PersonIcon from "@mui/icons-material/Person";
import VerifiedIcon from "@mui/icons-material/Verified";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { glossaryApi } from "../../api/client";
import { ui } from "../../theme/tokens";
import { useT } from "../../i18n";
import { useCanAuthorModel } from "../../auth/useCanAuthorModel";
import type {
  GlossaryConfidence,
  GlossaryBootstrapResponse,
  GlossaryEntry,
  GlossaryEntryUpdate,
  GlossarySource,
  GlossaryStatus,
  GlossaryVisibility,
} from "../../api/types";

function SourceBadge({ source }: { source: GlossarySource }) {
  const t = useT();
  if (source === "llm") {
    return (
      <Tooltip title={t("glossary.sourceLlmTitle")}>
        <Chip
          icon={<AutoAwesomeIcon />}
          label={t("glossary.sourceLlm")}
          size="small"
          color="warning"
          variant="outlined"
        />
      </Tooltip>
    );
  }
  if (source === "llm_approved") {
    return (
      <Tooltip title={t("glossary.sourceLlmApprovedTitle")}>
        <Chip
          icon={<VerifiedIcon />}
          label={t("glossary.sourceLlmApproved")}
          size="small"
          variant="outlined"
          sx={{ borderColor: ui.goldDark, color: ui.goldDark, "& .MuiChip-icon": { color: ui.goldDark } }}
        />
      </Tooltip>
    );
  }
  if (source === "heuristic") {
    return (
      <Tooltip title={t("glossary.sourceHeuristicTitle")}>
        <Chip
          icon={<WarningAmberIcon />}
          label={t("glossary.sourceHeuristic")}
          size="small"
          color="default"
          variant="outlined"
        />
      </Tooltip>
    );
  }
  return (
    <Tooltip title={t("glossary.sourceUserTitle")}>
      <Chip
        icon={<PersonIcon />}
        label={t("glossary.sourceUser")}
        size="small"
        color="success"
        variant="outlined"
      />
    </Tooltip>
  );
}

function formatSampleValue(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

interface EditState {
  entryId: string;
  term: string;
  definition: string;
  context_notes: string;
  synonyms: string;
  proposed_is_hidden: boolean;
  visibility: GlossaryVisibility;
  confidence: GlossaryConfidence;
}

export default function GlossaryPanel() {
  const t = useT();
  const { projectId, modelId } = useParams<{ projectId: string; modelId: string }>();
  const qc = useQueryClient();
  // F-026-04: gate every mutation entry point on the shared author capability.
  const canEdit = useCanAuthorModel();
  const [feedback, setFeedback] = useState<{
    severity: "success" | "error" | "info" | "warning";
    text: string;
  } | null>(null);
  const [editState, setEditState] = useState<EditState | null>(null);
  const [showRejected, setShowRejected] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkDeleteScope, setBulkDeleteScope] =
    useState<"all" | "heuristic" | "non_manual">("non_manual");
  const [bootstrapJobId, setBootstrapJobId] = useState<string | null>(null);
  // F-018-12: manual add-term, CSV import and revoke-only were API-complete but
  // had no UI. State + dialogs below expose all three.
  const [addTermOpen, setAddTermOpen] = useState(false);
  const [addTermState, setAddTermState] = useState<{
    term: string;
    definition: string;
    context_notes: string;
    synonyms: string;
  }>({ term: "", definition: "", context_notes: "", synonyms: "" });
  const [importOpen, setImportOpen] = useState(false);
  const [importCsvText, setImportCsvText] = useState("");
  const [confirmRevokeOpen, setConfirmRevokeOpen] = useState(false);

  const entries = useQuery({
    queryKey: ["glossary", projectId, modelId],
    queryFn: () => glossaryApi.list(projectId!, modelId!),
    enabled: !!projectId && !!modelId,
  });

  function applyBootstrapResult(data: GlossaryBootstrapResponse) {
    const parts = [t("glossary.bootstrapNew", { count: String(data.proposed_count) })];
    if (data.updated_count) parts.push(t("glossary.bootstrapUpdated", { count: String(data.updated_count) }));
    if (data.skipped_count) parts.push(t("glossary.bootstrapSkipped", { count: String(data.skipped_count) }));
    const countMsg = parts.join(" ");

    if (data.llm_error && !data.used_llm) {
      const provider = data.llm_provider ? ` (${data.llm_provider}/${data.llm_model})` : "";
      setFeedback({
        severity: "warning",
        text: `${t("glossary.bootstrapComplete")} ${countMsg} ${t("glossary.bootstrapEntriesHeuristicFallback")} ${t("glossary.bootstrapLlmError")}${provider}: ${data.llm_error}`,
      });
    } else if (data.used_llm) {
      const provider = data.llm_provider
        ? ` ${t("glossary.bootstrapVia", { source: `${data.llm_provider}/${data.llm_model}` })}`
        : "";
      const fb = data.fallback_count
        ? ` (${t("glossary.bootstrapHeuristicFallbacks", { count: String(data.fallback_count) })})`
        : "";
      const warn = data.llm_error ? ` ${t("glossary.bootstrapWarnings")}: ${data.llm_error}` : "";
      const severity = data.fallback_count ? "warning" as const : "success" as const;
      setFeedback({
        severity,
        text: `${t("glossary.bootstrapComplete")} ${countMsg}${provider}.${fb}${warn}`,
      });
    } else {
      setFeedback({
        severity: "info",
        text: `${t("glossary.bootstrapComplete")} ${countMsg} ${t("glossary.bootstrapEntriesHeuristicOnly")}`,
      });
    }
    qc.invalidateQueries({ queryKey: ["glossary", projectId, modelId] });
  }

  const bootstrapJob = useQuery({
    queryKey: ["glossaryBootstrapJob", projectId, modelId, bootstrapJobId],
    queryFn: () => glossaryApi.bootstrapJobStatus(projectId!, modelId!, bootstrapJobId!),
    enabled: !!projectId && !!modelId && !!bootstrapJobId,
    refetchInterval: bootstrapJobId ? 2000 : false,
  });

  useEffect(() => {
    const data = bootstrapJob.data;
    if (!data) return;
    if (data.job_status === "completed") {
      setBootstrapJobId(null);
      applyBootstrapResult(data);
    } else if (data.job_status === "failed") {
      setBootstrapJobId(null);
      setFeedback({ severity: "error", text: data.message || t("glossary.bootstrapFailed") });
    } else if (data.message) {
      setFeedback({ severity: "info", text: data.message });
    }
  }, [bootstrapJob.data]);

  const bootstrap = useMutation({
    mutationFn: () => glossaryApi.bootstrap(projectId!, modelId!),
    onSuccess: (data) => {
      if (data.job_id && data.job_status && data.job_status !== "completed") {
        setBootstrapJobId(data.job_id);
        setFeedback({ severity: "info", text: data.message || t("glossary.bootstrapping") });
        return;
      }
      applyBootstrapResult(data);
    },
    onError: () => setFeedback({ severity: "error", text: t("glossary.bootstrapFailed") }),
  });

  const bulkDelete = useMutation({
    mutationFn: (scope: "all" | "heuristic" | "non_manual") =>
      glossaryApi.deleteBulk(projectId!, modelId!, scope),
    onSuccess: (data) => {
      setBulkDeleteOpen(false);
      setFeedback({
        severity: "success",
        text: t("glossary.bulkDeleteDone", { count: String(data.deleted_count) }),
      });
      qc.invalidateQueries({ queryKey: ["glossary", projectId, modelId] });
    },
    onError: () => setFeedback({ severity: "error", text: t("glossary.bulkDeleteFailed") }),
  });

  const approveEntry = useMutation({
    mutationFn: (entryId: string) =>
      glossaryApi.approve(projectId!, modelId!, entryId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["glossary", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["tableAttributes"] });
    },
  });

  const approvePending = useMutation({
    mutationFn: () => glossaryApi.approveBulk(projectId!, modelId!),
    onSuccess: (data) => {
      // F-018-22: one transactional bulk approve instead of an N+1 loop.
      // Preserve the F-018-07 i18n keys with real {{ok}}/{{failed}} counts.
      setFeedback({
        severity: "success",
        text: t("glossary.approvedAllOk", { ok: data.approved_count.toString() }),
      });
      qc.invalidateQueries({ queryKey: ["glossary", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["tableAttributes"] });
    },
    onError: () =>
      setFeedback({
        severity: "error",
        text: t("glossary.approveAllFailed"),
      }),
  });

  const rejectEntry = useMutation({
    mutationFn: (entryId: string) =>
      glossaryApi.reject(projectId!, modelId!, entryId),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["glossary", projectId, modelId] }),
    onError: () => setFeedback({ severity: "error", text: t("glossary.failedToRejectEntry") }),
  });

  const deleteEntry = useMutation({
    mutationFn: (entryId: string) =>
      glossaryApi.delete(projectId!, modelId!, entryId),
    onSuccess: () => {
      setConfirmDeleteId(null);
      qc.invalidateQueries({ queryKey: ["glossary", projectId, modelId] });
    },
    onError: () => {
      setConfirmDeleteId(null);
      setFeedback({ severity: "error", text: t("glossary.failedToDeleteEntry") });
    },
  });

  const updateEntry = useMutation({
    mutationFn: (params: { entryId: string; data: GlossaryEntryUpdate }) =>
      glossaryApi.update(projectId!, modelId!, params.entryId, params.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["glossary", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["tableAttributes"] });
      setEditState(null);
    },
    onError: () => setFeedback({ severity: "error", text: t("glossary.failedToSaveChanges") }),
  });

  const shareGlossary = useMutation({
    mutationFn: () => glossaryApi.share(projectId!, modelId!),
    onSuccess: (data) => {
      const url = `${window.location.origin}${data.frontend_path}`;
      void navigator.clipboard?.writeText(url);
      setFeedback({
        severity: "info",
        text: `${t("glossary.shareLinkCopied")}: ${url}`,
      });
    },
    onError: () =>
      setFeedback({ severity: "error", text: t("glossary.failedToIssueShareLink") }),
  });

  const regenerateShareToken = useMutation({
    mutationFn: () => glossaryApi.regenerateShareToken(projectId!, modelId!),
    onSuccess: (data) => {
      const url = `${window.location.origin}${data.frontend_path}`;
      void navigator.clipboard?.writeText(url);
      setFeedback({
        severity: "success",
        text: `${t("glossary.regenerateSuccess")} ${url}`,
      });
    },
    onError: () =>
      setFeedback({
        severity: "error",
        text: t("glossary.failedToRegenerateLink"),
      }),
  });

  const addTerm = useMutation({
    mutationFn: () =>
      glossaryApi.create(projectId!, modelId!, {
        term: addTermState.term.trim(),
        definition: addTermState.definition.trim(),
        context_notes: addTermState.context_notes.trim() || null,
        synonyms: addTermState.synonyms
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        target_type: "concept",
      }),
    onSuccess: () => {
      setAddTermOpen(false);
      setAddTermState({ term: "", definition: "", context_notes: "", synonyms: "" });
      setFeedback({ severity: "success", text: t("glossary.addTermDone") });
      qc.invalidateQueries({ queryKey: ["glossary", projectId, modelId] });
    },
    onError: () => setFeedback({ severity: "error", text: t("glossary.addTermFailed") }),
  });

  const importCsv = useMutation({
    mutationFn: () => glossaryApi.importCsv(projectId!, modelId!, importCsvText),
    onSuccess: (data) => {
      setImportOpen(false);
      setImportCsvText("");
      const errorCount = data.errors?.length ?? 0;
      setFeedback({
        severity: errorCount ? "warning" : "success",
        text: errorCount
          ? t("glossary.importDoneWithErrors", {
              created: String(data.created),
              errors: String(errorCount),
            })
          : t("glossary.importDone", { created: String(data.created) }),
      });
      qc.invalidateQueries({ queryKey: ["glossary", projectId, modelId] });
    },
    onError: () => setFeedback({ severity: "error", text: t("glossary.importFailed") }),
  });

  const revokeShareTokens = useMutation({
    mutationFn: () => glossaryApi.revokeShareTokens(projectId!, modelId!),
    onSuccess: (data) => {
      setConfirmRevokeOpen(false);
      setFeedback({
        severity: "success",
        text: t("glossary.revokeDone", { count: String(data.revoked_count) }),
      });
    },
    onError: () => {
      setConfirmRevokeOpen(false);
      setFeedback({ severity: "error", text: t("glossary.revokeFailed") });
    },
  });

  const grouped = useMemo(() => {
    const out: Record<GlossaryStatus, GlossaryEntry[]> = {
      pending_review: [],
      approved: [],
      rejected: [],
    };
    for (const e of entries.data ?? []) {
      out[e.status]?.push(e);
    }
    return out;
  }, [entries.data]);

  function openEdit(entry: GlossaryEntry) {
    setEditState({
      entryId: entry.id,
      term: entry.term,
      definition: entry.definition,
      context_notes: entry.context_notes ?? "",
      synonyms: entry.synonyms.join(", "),
      proposed_is_hidden: entry.proposed_is_hidden ?? false,
      visibility: entry.visibility ?? "review",
      confidence: entry.confidence ?? "low",
    });
  }

  function saveEdit() {
    if (!editState) return;
    updateEntry.mutate({
      entryId: editState.entryId,
      data: {
        term: editState.term,
        definition: editState.definition,
        context_notes: editState.context_notes.trim() || null,
        synonyms: editState.synonyms
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        proposed_is_hidden: editState.proposed_is_hidden,
        visibility: editState.visibility,
        confidence: editState.confidence,
      },
    });
  }

  function approveAllPending() {
    // F-018-22: a single transactional bulk-approve replaces the previous
    // per-entry mutateAsync loop (one HTTP round trip per pending entry).
    setFeedback(null);
    approvePending.mutate();
  }

  const bootstrapRunning = bootstrap.isPending || !!bootstrapJobId;

  return (
    <Stack spacing={1.5}>
      <Box sx={{ p: 1.5, bgcolor: ui.mutedBg, borderRadius: 1, border: "1px solid", borderColor: "grey.200" }}>
        <Typography variant="caption" color="text.secondary" display="block" mb={0.5}>
          {t("glossary.description")}
        </Typography>
        <Typography variant="caption" color="text.secondary" display="block">
          {bootstrapRunning
            ? t("glossary.bootstrapping")
            : `${grouped.pending_review.length} ${t("glossary.statusPending")} • ${grouped.approved.length} ${t("glossary.statusApproved")} • ${grouped.rejected.length} ${t("glossary.statusRejected")}`}
        </Typography>
      </Box>

      {feedback && (
        <Alert severity={feedback.severity} onClose={() => setFeedback(null)}>
          {feedback.text}
        </Alert>
      )}

      {canEdit && (
      <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
        <Button
          variant="contained"
          size="small"
          startIcon={bootstrapRunning ? <CircularProgress size={16} color="inherit" /> : <AutoAwesomeIcon />}
          onClick={() => bootstrap.mutate()}
          disabled={bootstrapRunning}
        >
          {t("glossary.bootstrapButton")}
        </Button>
        <Button
          variant="outlined"
          size="small"
          startIcon={<AddIcon />}
          onClick={() => setAddTermOpen(true)}
        >
          {t("glossary.addTermButton")}
        </Button>
        <Button
          variant="outlined"
          size="small"
          startIcon={<UploadFileIcon />}
          onClick={() => setImportOpen(true)}
        >
          {t("glossary.importButton")}
        </Button>
        <Button
          variant="outlined"
          size="small"
          onClick={approveAllPending}
          disabled={
            grouped.pending_review.length === 0
            || approveEntry.isPending
            || approvePending.isPending
          }
        >
          {approvePending.isPending ? (
            <CircularProgress size={16} />
          ) : (
            t("glossary.approveAllButton")
          )}
        </Button>
        <Button
          variant="outlined"
          size="small"
          startIcon={shareGlossary.isPending ? <CircularProgress size={16} /> : <IosShareIcon />}
          onClick={() => shareGlossary.mutate()}
          disabled={shareGlossary.isPending}
        >
          {t("glossary.shareLinkButton")}
        </Button>
        <Tooltip title={t("glossary.regenerateLinkTooltip")}>
          <span>
            <Button
              variant="outlined"
              size="small"
              color="warning"
              onClick={() => regenerateShareToken.mutate()}
              disabled={regenerateShareToken.isPending}
            >
              {regenerateShareToken.isPending
                ? t("glossary.regenerating")
                : t("glossary.regenerateLinkButton")}
            </Button>
          </span>
        </Tooltip>
        <Button
          variant="outlined"
          size="small"
          color="warning"
          startIcon={<LinkOffIcon />}
          onClick={() => setConfirmRevokeOpen(true)}
          disabled={revokeShareTokens.isPending}
        >
          {t("glossary.revokeLinkButton")}
        </Button>
        <Button
          variant="outlined"
          size="small"
          color="error"
          startIcon={<DeleteOutlineIcon />}
          onClick={() => setBulkDeleteOpen(true)}
          disabled={(entries.data?.length ?? 0) === 0}
        >
          {t("glossary.bulkDeleteButton")}
        </Button>
      </Stack>
      )}

      {entries.isLoading && (
        <Box display="flex" justifyContent="center" py={3}>
          <CircularProgress size={24} />
        </Box>
      )}

      {entries.isSuccess && (entries.data?.length ?? 0) === 0 && (
        <Typography variant="body2" color="text.secondary">
          {t("glossary.noEntries")}
        </Typography>
      )}

      {(["pending_review", "approved", "rejected"] as GlossaryStatus[]).map((status) => {
        const items = grouped[status];
        if (items.length === 0) return null;
        const statusLabel = status === "pending_review" ? t("glossary.statusPendingReview") :
                            status === "approved" ? t("glossary.statusApproved") :
                            t("glossary.statusRejected");
        if (status === "rejected" && !showRejected) {
          return (
            <Box key={status}>
              <Button
                size="small"
                variant="text"
                onClick={() => setShowRejected(true)}
                sx={{ textTransform: "none" }}
              >
                {t("glossary.showRejected", { count: items.length.toString() })}
              </Button>
            </Box>
          );
        }
        return (
          <Box key={status}>
            <Box display="flex" alignItems="center" gap={1} mb={0.5}>
              <Typography variant="subtitle2" fontWeight={700}>
                {statusLabel} ({items.length})
              </Typography>
              {status === "rejected" && (
                <Button
                  size="small"
                  variant="text"
                  onClick={() => setShowRejected(false)}
                  sx={{ textTransform: "none", fontSize: 11, minWidth: 0, p: 0 }}
                >
                  {t("glossary.hideButton")}
                </Button>
              )}
            </Box>
            <Stack spacing={1}>
              {items.map((entry) => (
                <Box
                  key={entry.id}
                  sx={{
                    p: 1,
                    border: "1px solid",
                    borderColor: "divider",
                    borderRadius: 1,
                  }}
                >
                  <Box display="flex" alignItems="center" gap={1} mb={0.5}>
                    <Typography variant="body2" fontWeight={700} flexGrow={1}>
                      {entry.term}
                    </Typography>
                    <SourceBadge source={entry.source} />
                    <Chip
                      label={`v${entry.version}`}
                      size="small"
                      variant="outlined"
                      sx={{ fontSize: 10 }}
                    />
                    {entry.proposed_is_hidden && (
                      <Tooltip title={t("glossary.hideOnApproveTooltip")}>
                        <Chip label={t("glossary.hideOnApprove")} size="small" color="default" />
                      </Tooltip>
                    )}
                    {entry.visibility && (
                      <Tooltip title={t("glossary.visibilityRecommendation", { value: entry.visibility })}>
                        <Chip
                          label={entry.visibility === "show" ? t("glossary.visibilityShow") :
                                 entry.visibility === "hide" ? t("glossary.visibilityHide") :
                                 t("glossary.visibilityReview")}
                          size="small"
                          color={entry.visibility === "show" ? "success" :
                                 entry.visibility === "hide" ? "default" : "warning"}
                          variant="outlined"
                          sx={{ fontSize: 10 }}
                        />
                      </Tooltip>
                    )}
                    {entry.confidence && (
                      <Tooltip title={t("glossary.confidenceLevel", { value: entry.confidence })}>
                        <Chip
                          label={entry.confidence === "high" ? t("glossary.confidenceHigh") :
                                 entry.confidence === "medium" ? t("glossary.confidenceMedium") :
                                 t("glossary.confidenceLow")}
                          size="small"
                          color={entry.confidence === "high" ? "success" :
                                 entry.confidence === "medium" ? "info" : "warning"}
                          variant="outlined"
                          sx={{ fontSize: 10 }}
                        />
                      </Tooltip>
                    )}
                  </Box>
                  <Typography variant="caption" color="text.secondary" display="block">
                    {entry.definition}
                  </Typography>
                  {entry.context_notes && (
                    <Typography
                      variant="caption"
                      color="text.secondary"
                      display="block"
                      sx={{ mt: 0.5, fontStyle: "italic" }}
                    >
                      {t("glossary.contextLabel")}: {entry.context_notes}
                    </Typography>
                  )}
                  {entry.synonyms.length > 0 && (
                    <Box display="flex" gap={0.5} mt={0.5} flexWrap="wrap">
                      {entry.synonyms.map((s) => (
                        <Chip
                          key={s}
                          label={s}
                          size="small"
                          variant="outlined"
                          sx={{ fontSize: 10, height: 18 }}
                        />
                      ))}
                    </Box>
                  )}
                  {entry.sample_values && entry.sample_values.length > 0 && (
                    <Box display="flex" gap={0.5} mt={0.5} flexWrap="wrap" alignItems="center">
                      <Typography variant="caption" color="text.secondary">
                        {t("glossary.sampleValuesLabel")}:
                      </Typography>
                      {entry.sample_values.slice(0, 8).map((value, index) => (
                        <Chip
                          key={`${entry.id}-sample-${index}`}
                          label={formatSampleValue(value)}
                          size="small"
                          variant="outlined"
                          sx={{ fontSize: 10, height: 18, maxWidth: 180 }}
                        />
                      ))}
                    </Box>
                  )}
                  {entry.attachments.length > 0 && (
                    <Typography variant="caption" color="text.secondary" display="block" mt={0.5}>
                      {t("glossary.attachedToLabel")}{" "}
                      {entry.attachments
                        .map((a) =>
                          a.target_name
                            ? `${a.target_type}: ${a.target_name}`
                            : `${a.target_type}${a.target_id ? ` (${a.target_id.slice(0, 8)}…)` : ""}`,
                        )
                        .join(", ")}
                    </Typography>
                  )}
                  <Box display="flex" gap={0.5} mt={0.75} justifyContent="flex-end">
                    {canEdit && (
                    <>
                    {entry.status === "pending_review" && (
                      <Tooltip title={t("glossary.approveTooltip")}>
                        <IconButton
                          size="small"
                          color="success"
                          onClick={() => approveEntry.mutate(entry.id)}
                        >
                          <CheckIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    )}
                    <Tooltip title={t("glossary.editTooltip")}>
                      <IconButton size="small" onClick={() => openEdit(entry)}>
                        <EditIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                    {entry.status !== "rejected" && (
                      <Tooltip title={t("glossary.rejectTooltip")}>
                        <IconButton
                          size="small"
                          onClick={() => rejectEntry.mutate(entry.id)}
                        >
                          <CloseIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    )}
                    <Tooltip title={t("glossary.deleteTooltip")}>
                      <IconButton
                        size="small"
                        onClick={() => setConfirmDeleteId(entry.id)}
                      >
                        <DeleteOutlineIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                    </>
                    )}
                  </Box>
                </Box>
              ))}
            </Stack>
          </Box>
        );
      })}

      <Dialog open={!!confirmDeleteId} onClose={() => setConfirmDeleteId(null)}>
        <DialogTitle>{t("glossary.deleteTitle")}</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            {t("glossary.deleteConfirmMessage")}
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmDeleteId(null)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => confirmDeleteId && deleteEntry.mutate(confirmDeleteId)}
            disabled={deleteEntry.isPending}
          >
            {deleteEntry.isPending ? <CircularProgress size={16} /> : t("common.delete")}
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={!!editState} onClose={() => setEditState(null)} maxWidth="sm" fullWidth>
        <DialogTitle>{t("glossary.editTitle")}</DialogTitle>
        <DialogContent>
          {editState && (
            <Stack spacing={2} sx={{ mt: 1 }}>
              <TextField
                label={t("glossary.termLabel")}
                fullWidth
                size="small"
                value={editState.term}
                onChange={(e) => setEditState({ ...editState, term: e.target.value })}
              />
              <TextField
                label={t("glossary.definitionLabel")}
                fullWidth
                multiline
                minRows={2}
                maxRows={6}
                size="small"
                value={editState.definition}
                onChange={(e) => setEditState({ ...editState, definition: e.target.value })}
              />
              <TextField
                label={t("glossary.contextNotesLabel")}
                fullWidth
                multiline
                minRows={1}
                maxRows={4}
                size="small"
                value={editState.context_notes}
                onChange={(e) => setEditState({ ...editState, context_notes: e.target.value })}
                helperText={t("glossary.contextNotesHelper")}
              />
              <TextField
                label={t("glossary.synonymsLabel")}
                fullWidth
                size="small"
                value={editState.synonyms}
                onChange={(e) => setEditState({ ...editState, synonyms: e.target.value })}
              />
              <FormControlLabel
                control={
                  <Checkbox
                    checked={editState.proposed_is_hidden}
                    onChange={(e) =>
                      setEditState({ ...editState, proposed_is_hidden: e.target.checked })
                    }
                  />
                }
                label={t("glossary.hideColumnLabel")}
              />
              <Typography variant="caption" color="text.secondary" display="block" mt={-1} mb={0.5}>
                {t("glossary.hideColumnHelp")}
              </Typography>
              <Stack direction="row" spacing={2}>
                <FormControl size="small" sx={{ minWidth: 140 }}>
                  <InputLabel>{t("glossary.visibilityLabel")}</InputLabel>
                  <Select
                    label={t("glossary.visibilityLabel")}
                    value={editState.visibility}
                    onChange={(e) =>
                      setEditState({
                        ...editState,
                        visibility: e.target.value as GlossaryVisibility,
                      })
                    }
                  >
                    <MenuItem value="show">{t("glossary.visibilityShow")}</MenuItem>
                    <MenuItem value="hide">{t("glossary.visibilityHide")}</MenuItem>
                    <MenuItem value="review">{t("glossary.visibilityReview")}</MenuItem>
                  </Select>
                </FormControl>
                <FormControl size="small" sx={{ minWidth: 140 }}>
                  <InputLabel>{t("glossary.confidenceLabel")}</InputLabel>
                  <Select
                    label={t("glossary.confidenceLabel")}
                    value={editState.confidence}
                    onChange={(e) =>
                      setEditState({
                        ...editState,
                        confidence: e.target.value as GlossaryConfidence,
                      })
                    }
                  >
                    <MenuItem value="high">{t("glossary.confidenceHighOption")}</MenuItem>
                    <MenuItem value="medium">{t("glossary.confidenceMediumOption")}</MenuItem>
                    <MenuItem value="low">{t("glossary.confidenceLowOption")}</MenuItem>
                  </Select>
                </FormControl>
              </Stack>
            </Stack>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setEditState(null)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={saveEdit}
            disabled={updateEntry.isPending}
          >
            {updateEntry.isPending ? <CircularProgress size={16} /> : t("common.save")}
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={bulkDeleteOpen} onClose={() => setBulkDeleteOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle>{t("glossary.bulkDeleteTitle")}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            {t("glossary.bulkDeletePrompt")}
          </Typography>
          <FormControl fullWidth size="small">
            <InputLabel>{t("glossary.bulkDeleteScopeLabel")}</InputLabel>
            <Select
              label={t("glossary.bulkDeleteScopeLabel")}
              value={bulkDeleteScope}
              onChange={(e) =>
                setBulkDeleteScope(e.target.value as "all" | "heuristic" | "non_manual")
              }
            >
              <MenuItem value="all">{t("glossary.bulkDeleteScopeAll")}</MenuItem>
              <MenuItem value="heuristic">{t("glossary.bulkDeleteScopeHeuristic")}</MenuItem>
              <MenuItem value="non_manual">{t("glossary.bulkDeleteScopeNonManual")}</MenuItem>
            </Select>
          </FormControl>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setBulkDeleteOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            color="error"
            onClick={() => bulkDelete.mutate(bulkDeleteScope)}
            disabled={bulkDelete.isPending}
          >
            {bulkDelete.isPending ? <CircularProgress size={16} /> : t("glossary.bulkDeleteConfirm")}
          </Button>
        </DialogActions>
      </Dialog>

      {/* F-018-12: manual Add-term dialog (target_type=concept). */}
      <Dialog open={addTermOpen} onClose={() => setAddTermOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{t("glossary.addTermTitle")}</DialogTitle>
        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label={t("glossary.termLabel")}
              fullWidth
              size="small"
              value={addTermState.term}
              onChange={(e) => setAddTermState({ ...addTermState, term: e.target.value })}
            />
            <TextField
              label={t("glossary.definitionLabel")}
              fullWidth
              multiline
              minRows={2}
              maxRows={6}
              size="small"
              value={addTermState.definition}
              onChange={(e) => setAddTermState({ ...addTermState, definition: e.target.value })}
            />
            <TextField
              label={t("glossary.contextNotesLabel")}
              fullWidth
              multiline
              minRows={1}
              maxRows={4}
              size="small"
              value={addTermState.context_notes}
              onChange={(e) => setAddTermState({ ...addTermState, context_notes: e.target.value })}
            />
            <TextField
              label={t("glossary.synonymsLabel")}
              fullWidth
              size="small"
              value={addTermState.synonyms}
              onChange={(e) => setAddTermState({ ...addTermState, synonyms: e.target.value })}
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setAddTermOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => addTerm.mutate()}
            disabled={
              addTerm.isPending ||
              !addTermState.term.trim() ||
              !addTermState.definition.trim()
            }
          >
            {addTerm.isPending ? <CircularProgress size={16} /> : t("common.add")}
          </Button>
        </DialogActions>
      </Dialog>

      {/* F-018-12: CSV import dialog. */}
      <Dialog open={importOpen} onClose={() => setImportOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{t("glossary.importTitle")}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            {t("glossary.importPrompt")}
          </Typography>
          <TextField
            fullWidth
            multiline
            minRows={6}
            maxRows={14}
            size="small"
            placeholder={"term,description\n..."}
            value={importCsvText}
            onChange={(e) => setImportCsvText(e.target.value)}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setImportOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => importCsv.mutate()}
            disabled={importCsv.isPending || !importCsvText.trim()}
          >
            {importCsv.isPending ? <CircularProgress size={16} /> : t("glossary.importConfirm")}
          </Button>
        </DialogActions>
      </Dialog>

      {/* F-018-12: revoke-only confirm — kill a leaked link without minting a new one. */}
      <Dialog open={confirmRevokeOpen} onClose={() => setConfirmRevokeOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle>{t("glossary.revokeTitle")}</DialogTitle>
        <DialogContent>
          <Typography variant="body2">{t("glossary.revokePrompt")}</Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmRevokeOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            color="warning"
            onClick={() => revokeShareTokens.mutate()}
            disabled={revokeShareTokens.isPending}
          >
            {revokeShareTokens.isPending ? <CircularProgress size={16} /> : t("glossary.revokeConfirm")}
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}
