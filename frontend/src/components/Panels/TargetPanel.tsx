import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import EditIcon from "@mui/icons-material/Edit";
import DeleteIcon from "@mui/icons-material/Delete";
import { targetsApi } from "../../api/client";
import { useConnections, useTargets } from "../../api/hooks";
import type { TargetCreate } from "../../api/types";
import { useConfirm } from "../Confirm";
import { useT } from "../../i18n";

export default function TargetPanel() {
  const { projectId, modelId } = useParams<{
    projectId: string;
    modelId: string;
  }>();
  const qc = useQueryClient();
  const t = useT();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [stName, setStName] = useState("");
  const [stType, setStType] = useState("postgresql");
  const [stConnId, setStConnId] = useState("");
  const [stSchema, setStSchema] = useState("");

  const targets = useTargets(projectId!, modelId!);
  const connections = useConnections(projectId!);

  const currentTarget = targets.data?.[0] ?? null;

  const saveTarget = useMutation({
    mutationFn: async () => {
      const config: Record<string, unknown> = {};
      if (stSchema) {
        if (stType === "bigquery") {
          config.dataset = stSchema;
        } else {
          config.schema = stSchema;
        }
      }
      if (currentTarget) {
        return targetsApi.update(projectId!, modelId!, currentTarget.id, {
          project_connection_id: stConnId,
          target_type: stType,
          display_name: stName,
          config,
        });
      }
      const data: TargetCreate = {
        project_connection_id: stConnId,
        target_type: stType,
        display_name: stName,
        config,
      };
      return targetsApi.create(projectId!, modelId!, data);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["targets", projectId, modelId] });
      setDialogOpen(false);
    },
  });

  const deleteTarget = useMutation({
    mutationFn: (targetId: string) =>
      targetsApi.delete(projectId!, modelId!, targetId),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["targets", projectId, modelId] }),
  });

  const confirm = useConfirm();
  async function handleClearTarget(id: string) {
    const ok = await confirm({
      title: t("target.clearTitle"),
      message: t("target.clearMessage"),
      confirmLabel: t("target.clearTarget"),
    });
    if (ok) deleteTarget.mutate(id);
  }

  function openDialog(prefill = false) {
    if (prefill && currentTarget) {
      setStName(currentTarget.display_name);
      setStType(currentTarget.target_type);
      setStConnId(currentTarget.project_connection_id);
      setStSchema(
        String(
          (currentTarget.config as Record<string, unknown>)?.schema ??
          (currentTarget.config as Record<string, unknown>)?.dataset ?? ""
        )
      );
    } else {
      setStName("");
      setStType("postgresql");
      setStConnId("");
      setStSchema("");
    }
    setDialogOpen(true);
  }

  function handleConnChange(connId: string) {
    setStConnId(connId);
    const conn = connections.data?.find((c) => c.id === connId);
    if (conn) {
      // Phase C legacy fallback: map the historical ``jdbc`` label onto
      // ``hadoop_spark`` so a target created against a legacy connection
      // gets the canonical type. All new connections are already
      // ``hadoop_spark`` after the unification.
      const mapped =
        conn.connection_type === "jdbc" ? "hadoop_spark" : conn.connection_type;
      setStType(mapped);
    }
  }

  return (
    <Box>
      <Box display="flex" alignItems="center" mb={1.5}>
        <Typography variant="body2" color="text.secondary" sx={{ flex: 1 }}>
          {t("target.description")}
        </Typography>
      </Box>

      {targets.isLoading ? (
        <CircularProgress size={20} />
      ) : currentTarget ? (
        /* Show the single assigned target */
        <Card variant="outlined">
          <CardContent
            sx={{
              py: 1,
              "&:last-child": { pb: 1 },
              display: "flex",
              alignItems: "center",
              gap: 1,
            }}
          >
            <Box flexGrow={1}>
              <Typography variant="body2" fontWeight={600}>
                {currentTarget.display_name}
              </Typography>
              <Typography variant="caption" color="text.secondary" display="block" mt={0.25}>
                {currentTarget.target_type}
                {(() => {
                  const schema =
                    (currentTarget.config as Record<string, unknown>)?.schema ??
                    (currentTarget.config as Record<string, unknown>)?.dataset ??
                    null;
                  return schema ? ` · ${String(schema)}` : "";
                })()}
              </Typography>
            </Box>
            <Tooltip title={t("target.editTargetTooltip")}>
              <IconButton size="small" onClick={() => openDialog(true)}>
                <EditIcon fontSize="small" />
              </IconButton>
            </Tooltip>
            <Tooltip title={t("target.clearTargetTooltip")}>
              <IconButton
                size="small"
                onClick={() => handleClearTarget(currentTarget.id)}
              >
                <DeleteIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          </CardContent>
        </Card>
      ) : (
        /* No target assigned yet */
        <Box textAlign="center" py={3}>
          <Typography variant="body2" color="text.secondary" mb={2}>
            {t("target.noTarget")}
          </Typography>
          <Button variant="contained" onClick={() => openDialog()}>
            {t("target.setTarget")}
          </Button>
        </Box>
      )}

      {/* Create / Replace Target Dialog */}
      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>
          {currentTarget ? t("target.editTarget") : t("target.setTarget")}
        </DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" mb={1}>
            {t("target.dialogDescription")}
          </Typography>

          <TextField
            label={t("target.displayName")}
            fullWidth
            margin="normal"
            value={stName}
            onChange={(e) => setStName(e.target.value)}
            autoFocus
          />
          <FormControl fullWidth margin="normal">
            <InputLabel>{t("target.connection")}</InputLabel>
            <Select
              value={stConnId}
              label={t("target.connection")}
              onChange={(e) => handleConnChange(e.target.value)}
            >
              {connections.data?.map((c) => (
                <MenuItem key={c.id} value={c.id}>
                  {c.display_name} ({c.connection_type})
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl fullWidth margin="normal">
            <InputLabel>{t("target.type")}</InputLabel>
            <Select
              value={stType}
              label={t("target.type")}
              onChange={(e) => setStType(e.target.value)}
            >
              <MenuItem value="postgresql">{t("connectionType.postgresql")}</MenuItem>
              <MenuItem value="bigquery">{t("connectionType.bigquery")}</MenuItem>
              <MenuItem value="hadoop_spark">{t("connectionType.hadoopSpark")}</MenuItem>
              <MenuItem value="redshift">{t("connectionType.redshift")}</MenuItem>
              <MenuItem value="snowflake">{t("connectionType.snowflake")}</MenuItem>
              <MenuItem value="sqlserver">{t("connectionType.sqlserver")}</MenuItem>
            </Select>
          </FormControl>
          <TextField
            label={stType === "bigquery" ? t("target.dataset") : t("target.schema")}
            fullWidth
            margin="normal"
            value={stSchema}
            onChange={(e) => setStSchema(e.target.value)}
            placeholder={
              stType === "bigquery" ? t("target.schemaPlaceholderBigquery") : t("target.schemaPlaceholder")
            }
            helperText={t("target.schemaHelp")}
          />

          {saveTarget.isError && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {currentTarget ? t("target.saveFailed") : t("target.setFailed")}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialogOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => saveTarget.mutate()}
            disabled={!stName || !stConnId || saveTarget.isPending}
          >
            {saveTarget.isPending ? (
              <CircularProgress size={18} />
            ) : currentTarget ? (
              t("common.save")
            ) : (
              t("target.setTarget")
            )}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
