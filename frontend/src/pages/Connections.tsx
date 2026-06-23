import { useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
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
  Stack,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import DeleteIcon from "@mui/icons-material/Delete";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import { connectionsApi } from "../api/client";
import { useConnections } from "../api/hooks";
import type { ConnectionCreate } from "../api/types";
import {
  CONN_FIELDS,
  buildJsonFromFields,
  getDefaultValues,
  isFieldVisible,
  type ConnField,
} from "../components/connectionFields";
import { renderConnFields } from "../components/renderConnFields";
import HelpIconButton from "../components/HelpIconButton";
import { useT } from "../i18n";

export default function Connections() {
  const t = useT();
  const { tenantId, projectId } = useParams<{
    tenantId: string;
    projectId: string;
  }>();
  const navigate = useNavigate();
  const qc = useQueryClient();

  const [connOpen, setConnOpen] = useState(false);
  const [connName, setConnName] = useState("");
  const [connType, setConnType] =
    useState<ConnectionCreate["connection_type"]>("postgresql");
  const [connFields, setConnFields] = useState<Record<string, string>>({});
  const [testResult, setTestResult] = useState<string | null>(null);

  const connections = useConnections(projectId!);

  function setField(key: string, value: string) {
    setConnFields((prev) => ({ ...prev, [key]: value }));
  }

  function resetConnDialog(type: ConnectionCreate["connection_type"]) {
    setConnName("");
    setConnType(type);
    setConnFields(getDefaultValues(CONN_FIELDS[type] ?? []));
  }

  function isConnFormValid(): boolean {
    const fields = CONN_FIELDS[connType] ?? [];
    if (!connName) return false;
    for (const f of fields) {
      if (!f.required) continue;
      if (!isFieldVisible(f, connFields)) continue;
      const val = connFields[f.key] ?? f.defaultValue ?? "";
      if (!val) return false;
    }
    return true;
  }

  // Mutations
  const createConn = useMutation({
    mutationFn: () => {
      const fields = CONN_FIELDS[connType] ?? [];
      const creds = buildJsonFromFields(fields, connFields, "credentials");
      const cfg = buildJsonFromFields(fields, connFields, "config");
      return connectionsApi.create(projectId!, {
        display_name: connName,
        connection_type: connType,
        credentials: creds,
        config: cfg,
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["connections", projectId] });
      setConnOpen(false);
    },
  });

  const testConn = useMutation({
    mutationFn: (connId: string) => connectionsApi.test(projectId!, connId),
    onSuccess: () => setTestResult(t("connections.connectionSuccessful")),
    onError: () => setTestResult(t("connections.connectionFailed")),
  });

  const deleteConn = useMutation({
    mutationFn: (id: string) => connectionsApi.delete(projectId!, id),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["connections", projectId] }),
  });

  return (
    <Box>
      <Box display="flex" alignItems="center" gap={1} mb={2}>
        <Tooltip title={t("connections.backToWorkspace")}>
          <IconButton onClick={() => navigate("/")}>
            <ArrowBackIcon />
          </IconButton>
        </Tooltip>
        <Typography variant="h5" fontWeight={700}>
          {t("connections.title")}
        </Typography>
        <HelpIconButton href="/help/modelling/manage-connections.html" />
      </Box>

      <Box display="flex" mb={2}>
        <Box flexGrow={1} />
        <Button
          variant="contained"
          startIcon={<AddIcon />}
          onClick={() => {
            resetConnDialog("postgresql");
            setConnOpen(true);
          }}
        >
          {t("connections.addConnection")}
        </Button>
      </Box>

      {connections.isLoading ? (
        <CircularProgress />
      ) : (
        <Stack spacing={1.5}>
          {connections.data?.map((c) => (
            <Card key={c.id} variant="outlined">
              <CardContent
                sx={{
                  py: 1.5,
                  display: "flex",
                  alignItems: "center",
                  gap: 2,
                }}
              >
                <Box flexGrow={1}>
                  <Typography fontWeight={600}>{c.display_name}</Typography>
                  <Chip label={c.connection_type} size="small" />
                </Box>
                <Tooltip title={t("connections.testConnection")}>
                  <IconButton
                    size="small"
                    onClick={() => {
                      setTestResult(null);
                      testConn.mutate(c.id);
                    }}
                  >
                    <PlayArrowIcon />
                  </IconButton>
                </Tooltip>
                <Tooltip title={t("connections.deleteConnection")}>
                  <IconButton
                    size="small"
                    onClick={() => deleteConn.mutate(c.id)}
                  >
                    <DeleteIcon />
                  </IconButton>
                </Tooltip>
              </CardContent>
            </Card>
          ))}
          {connections.data?.length === 0 && (
            <Typography variant="body2" color="text.secondary">
              {t("connections.noConnections")}
            </Typography>
          )}
          {testResult && (
            <Alert
              severity={
                testResult.includes("successful") ? "success" : "error"
              }
              icon={
                testResult.includes("successful") ? (
                  <CheckCircleIcon />
                ) : undefined
              }
            >
              {testResult}
            </Alert>
          )}
        </Stack>
      )}

      {/* Add Connection Dialog */}
      <Dialog
        open={connOpen}
        onClose={() => setConnOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>{t("connections.addTitle")}</DialogTitle>
        <DialogContent>
          <TextField
            label={t("connections.displayName")}
            fullWidth
            margin="normal"
            value={connName}
            onChange={(e) => setConnName(e.target.value)}
            autoFocus
          />
          <FormControl fullWidth margin="normal">
            <InputLabel>{t("connections.connectionType")}</InputLabel>
            <Select
              value={connType}
              label={t("connections.connectionType")}
              onChange={(e) => {
                const newType = e.target.value as ConnectionCreate["connection_type"];
                resetConnDialog(newType);
                setConnType(newType);
              }}
            >
              <MenuItem value="postgresql">{t("connections.connectionTypePostgresql")}</MenuItem>
              <MenuItem value="bigquery">{t("connections.connectionTypeBigquery")}</MenuItem>
              <MenuItem value="hadoop_spark">{t("connections.connectionTypeHadoopSpark")}</MenuItem>
            </Select>
          </FormControl>

          <Typography
            variant="subtitle2"
            color="text.secondary"
            mt={2}
            mb={0.5}
          >
            {t("connections.connectionDetails")}
          </Typography>

          {renderConnFields(CONN_FIELDS[connType] ?? [], connFields, setField, t)}

          {createConn.isError && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {t("connections.createError")}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConnOpen(false)}>{t("connections.cancelButton")}</Button>
          <Button
            variant="contained"
            onClick={() => createConn.mutate()}
            disabled={!isConnFormValid() || createConn.isPending}
          >
            {createConn.isPending ? <CircularProgress size={18} /> : t("connections.createButton")}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
}
