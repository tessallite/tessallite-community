import { useState } from "react";
import { useT } from "../../i18n";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
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
import StarIcon from "@mui/icons-material/Star";
import { versionsApi } from "../../api/versionsApi";
import { useConfirm } from "../Confirm";
import { useModelEditorStore } from "../../store/useModelEditorStore";
import VersionDiffPanel from "./VersionDiffPanel";

type Props = {
  open: boolean;
  onClose: () => void;
  projectId: string;
  modelId: string;
};

/**
 * Lists every saved version for the model, with Deploy + Revert actions.
 * Revert deletes every newer version (typed-name confirm); Deploy points
 * the runtime at the chosen version.
 */
export default function VersionsDialog({ open, onClose, projectId, modelId }: Props) {
  const t = useT();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const markClean = useModelEditorStore((s) => s.markClean);
  const [activeTab, setActiveTab] = useState<"history" | "diff">("history");

  const versions = useQuery({
    queryKey: ["versions", projectId, modelId],
    queryFn: () => versionsApi.list(projectId, modelId),
    enabled: open,
  });

  const deployMut = useMutation({
    mutationFn: (versionId: string) =>
      versionsApi.deploy(projectId, modelId, versionId),
    onSuccess: (data) => {
      markClean({
        deployedVersion:
          versions.data?.find((v) => v.id === data.deployed_version_id)
            ?.version_number ?? null,
        lastDeployedAt: data.last_deployed_at,
      });
      qc.invalidateQueries({ queryKey: ["versions", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["models", projectId, modelId] });
    },
  });

  const revertMut = useMutation({
    mutationFn: (args: { versionId: string; confirm: string }) =>
      versionsApi.revert(projectId, modelId, args.versionId, args.confirm),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["versions", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["models", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["sources", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["dimensions", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["measures", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["joins", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["hierarchies", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["aggregates", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["allModelTables", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["modelTables", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["tableAttributes", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["personas", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["pockets", projectId, modelId] });
      qc.invalidateQueries({ queryKey: ["glossary", projectId, modelId] });
      markClean();
    },
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
    // F-013-14: the typed-confirmation phrase comes from the prepared
    // `versions.revertConfirmText` key rather than a hardcoded string, so the
    // dialog prompt and the value sent to the backend stay in lockstep with
    // i18n. The backend phrase contract is matched case-insensitively
    // (versions.py), so a localized key still works as long as it interpolates
    // the version number.
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
    if (ok) revertMut.mutate({ versionId, confirm: confirmPhrase });
  }

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
          <Tab label={t("versions.diffTab")} value="diff" disabled={!versions.data || versions.data.length < 2} />
        </Tabs>
      </Box>
      <DialogContent>
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
        {activeTab === "diff" && versions.data && (
          <VersionDiffPanel
            projectId={projectId}
            modelId={modelId}
            versions={versions.data}
          />
        )}
        {activeTab === "history" && versions.data && versions.data.length > 0 && (
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
                {versions.data.map((v) => (
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
                      {v.is_deployed && (
                        <Chip
                          icon={<StarIcon />}
                          label={t("versions.deployed")}
                          color="success"
                          size="small"
                        />
                      )}
                    </TableCell>
                    <TableCell align="right">
                      <Stack direction="row" spacing={1} justifyContent="flex-end">
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
                        <Button
                          size="small"
                          variant="outlined"
                          onClick={() => handleRevert(v.id, v.version_number)}
                          disabled={revertMut.isPending}
                        >
                          {t("versions.revertButton")}
                        </Button>
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
