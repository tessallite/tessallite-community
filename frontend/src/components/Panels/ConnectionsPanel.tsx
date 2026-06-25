import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import {
  Box,
  Button,
  Card,
  CardContent,
  CircularProgress,
  IconButton,
  List,
  ListItem,
  ListItemText,
  Tooltip,
  Typography,
} from "@mui/material";
import ConnectionDialog from "./ConnectionDialog";
import AddIcon from "@mui/icons-material/Add";
import CableIcon from "@mui/icons-material/Cable";
import DeleteIcon from "@mui/icons-material/Delete";
import EditIcon from "@mui/icons-material/Edit";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import { connectionsApi } from "../../api/client";
import { useConnections } from "../../api/hooks";
import type { ConnectionCreate } from "../../api/types";
import { useBuilderStore } from "../../store/builderStore";
import {
  CONN_FIELDS,
  buildJsonFromFields,
  getDefaultValues,
  isFieldVisible,
} from "../connectionFields";
import { useConfirm } from "../Confirm";
import { canPerform } from "../../auth/explorerPrivileges";

export default function ConnectionsPanel({
  projectId: propProjectId,
}: { projectId?: string } = {}) {
  const { projectId: routeProjectId } = useParams<{ projectId: string }>();
  const projectId = propProjectId ?? routeProjectId;
  const qc = useQueryClient();
  const connections = useConnections(projectId!);
  const t = useT();
  // Bug-5445: connection create/test/edit/delete write credentials and are
  // admin-only on the backend (api/connections.py require_role("admin")). Gate
  // the management controls to admin, mirroring the project-setup-drawer's
  // admin-only Connections section. The list/get view stays visible to modeller
  // (require_role("modeler")) so they can still pick a connection for a source.
  const canManageConnections = canPerform("connection.manage");

  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [connType, setConnType] =
    useState<ConnectionCreate["connection_type"]>("postgresql");
  const [fields, setFields] = useState<Record<string, string>>(
    getDefaultValues(CONN_FIELDS["postgresql"] ?? []),
  );
  const [draftTestResult, setDraftTestResult] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const [editType, setEditType] = useState<string>("postgresql");
  const [editFields, setEditFields] = useState<Record<string, string>>({});
  // Stores config keys present on the loaded connection but not in
  // CONN_FIELDS (e.g. the seed-written "name" tag). The form merges
  // these back into the saved config so editing schema/write_access
  // doesn't wipe them.
  const [editPassthroughConfig, setEditPassthroughConfig] = useState<Record<string, unknown>>({});
  const [editTestResult, setEditTestResult] = useState<string | null>(null);
  const [savedTestResult, setSavedTestResult] = useState<Record<string, string>>({});
  const setGlobalMessage = useBuilderStore((s) => s.setGlobalMessage);

  const CONN_TYPE_KEYS: Record<string, string> = {
    postgresql: "connectionType.postgresql",
    bigquery: "connectionType.bigquery",
    hadoop_spark: "connectionType.hadoopSpark",
    redshift: "connectionType.redshift",
    snowflake: "connectionType.snowflake",
    sqlserver: "connectionType.sqlserver",
    jdbc: "connectionType.jdbc",
  };
  function connTypeLabel(cType: string) {
    return t(CONN_TYPE_KEYS[cType] ?? cType);
  }

  function resetForm(type: ConnectionCreate["connection_type"]) {
    setName("");
    setConnType(type);
    setFields(getDefaultValues(CONN_FIELDS[type] ?? []));
    setDraftTestResult(null);
  }

  function isValid(): boolean {
    const defs = CONN_FIELDS[connType] ?? [];
    if (!name) return false;
    for (const f of defs) {
      if (!f.required || !isFieldVisible(f, fields)) continue;
      if (!(fields[f.key] ?? f.defaultValue ?? "")) return false;
    }
    return true;
  }

  function getDraftPayload() {
    const defs = CONN_FIELDS[connType] ?? [];
    return {
      display_name: name,
      connection_type: connType,
      credentials: buildJsonFromFields(defs, fields, "credentials"),
      config: buildJsonFromFields(defs, fields, "config"),
    };
  }

  const create = useMutation({
    mutationFn: () => connectionsApi.create(projectId!, getDraftPayload()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["connections", projectId] });
      setOpen(false);
      resetForm("postgresql");
    },
  });

  const testDraft = useMutation({
    mutationFn: () => connectionsApi.testDraft(projectId!, getDraftPayload()),
    onSuccess: (result) => {
      setDraftTestResult(result.ok ? "success" : result.detail ?? "failed");
      setGlobalMessage(
        result.ok ? t("connections.draftTestPassed") : t("connections.draftTestFailed", { detail: result.detail ?? "failed" }),
        result.ok ? "success" : "error",
      );
    },
    onError: (error: unknown) => {
      setDraftTestResult(error instanceof Error ? error.message : "failed");
      setGlobalMessage(
        error instanceof Error ? t("connections.draftTestFailed", { detail: error.message }) : t("connections.draftTestFailed", { detail: "" }),
        "error",
      );
    },
  });

  const testSaved = useMutation({
    mutationFn: (connId: string) => connectionsApi.test(projectId!, connId),
    onSuccess: (result, connId) => {
      setSavedTestResult((prev) => ({
        ...prev,
        [connId]: result.ok ? "success" : result.detail ?? "failed",
      }));
      setGlobalMessage(
        result.ok ? t("connections.testPassed") : t("connections.testFailed", { error: result.detail ?? "failed" }),
        result.ok ? "success" : "error",
      );
    },
    onError: (error: unknown, connId) => {
      setSavedTestResult((prev) => ({
        ...prev,
        [connId]: error instanceof Error ? error.message : "failed",
      }));
      setGlobalMessage(
        error instanceof Error ? t("connections.testFailed", { error: error.message }) : t("connections.testFailed", { error: "" }),
        "error",
      );
    },
  });

  const deleteConn = useMutation({
    mutationFn: (id: string) => connectionsApi.delete(projectId!, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["connections", projectId] }),
    onError: (error: unknown) => {
      // F-014-05: FastAPI nests the structured 409 body under `detail`, so the
      // dependent-models list lives at response.data.detail.dependent_models —
      // reading response.data.dependent_models was one level too shallow and the
      // "used by Sales Model, Finance Model" message never reached the user.
      const detail = (error as {
        response?: { data?: { detail?: { message?: string; dependent_models?: string[] } } };
      })?.response?.data?.detail;
      if (detail?.dependent_models?.length) {
        setGlobalMessage(
          t("connections.cannotDelete", { models: detail.dependent_models.join(", ") }),
          "error",
        );
      } else {
        setGlobalMessage(t("connections.deleteFailed"), "error");
      }
    },
  });

  const confirm = useConfirm();
  async function handleDeleteConn(c: { id: string; display_name: string }) {
    const ok = await confirm({
      mode: "typed-name",
      title: t("connections.deleteTitle"),
      message: (
        <span>
          {t("connections.deleteMessage", { name: c.display_name })}
        </span>
      ),
      confirmText: c.display_name,
      confirmLabel: t("connections.deleteConfirmLabel"),
    });
    if (ok) deleteConn.mutate(c.id);
  }

  function getEditPayload() {
    const defs = CONN_FIELDS[editType] ?? [];
    const payload: {
      display_name: string;
      connection_type?: ConnectionCreate["connection_type"];
      credentials?: Record<string, unknown>;
      config?: Record<string, unknown>;
    } = { display_name: editName, connection_type: editType as ConnectionCreate["connection_type"] };
    const creds = buildJsonFromFields(defs, editFields, "credentials");
    if (Object.keys(creds).length > 0) payload.credentials = creds;
    // Only send config when the connector defines config-group fields,
    // otherwise we would wipe any config the backend already stored.
    // Merge passthrough keys (unknown to the form) so they survive a save.
    if (defs.some((f) => f.group === "config")) {
      payload.config = {
        ...editPassthroughConfig,
        ...buildJsonFromFields(defs, editFields, "config"),
      };
    }
    return payload;
  }

  function isEditValid(): boolean {
    const defs = CONN_FIELDS[editType] ?? [];
    if (!editName) return false;
    for (const f of defs) {
      if (!f.required || !isFieldVisible(f, editFields)) continue;
      // Password may be left blank on edit to keep the stored value.
      if (f.type === "password") continue;
      if (!(editFields[f.key] ?? f.defaultValue ?? "")) return false;
    }
    return true;
  }

  function openEdit(c: {
    id: string;
    display_name: string;
    connection_type: string;
    config?: Record<string, unknown>;
    credentials_preview?: Record<string, unknown>;
  }) {
    const type = c.connection_type;
    const defs = CONN_FIELDS[type] ?? [];
    const initial: Record<string, string> = {};
    const merged = { ...(c.credentials_preview ?? {}), ...(c.config ?? {}) };
    for (const f of defs) {
      const v = merged[f.key];
      if (v !== undefined && v !== null) initial[f.key] = String(v);
      else if (f.defaultValue) initial[f.key] = f.defaultValue;
    }
    // Capture config keys not represented in the form so we can re-emit
    // them on save (otherwise toggling write_access would wipe them).
    const formConfigKeys = new Set(defs.filter((f) => f.group === "config").map((f) => f.key));
    const passthrough: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(c.config ?? {})) {
      if (!formConfigKeys.has(k)) passthrough[k] = v;
    }
    setEditingId(c.id);
    setEditName(c.display_name);
    setEditType(type);
    setEditFields(initial);
    setEditPassthroughConfig(passthrough);
    setEditTestResult(null);
  }

  const updateConn = useMutation({
    mutationFn: () =>
      connectionsApi.update(projectId!, editingId!, getEditPayload()),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["connections", projectId] });
      setEditingId(null);
    },
  });

  const testEdit = useMutation({
    mutationFn: () => {
      const p = getEditPayload();
      return connectionsApi.testEdit(projectId!, editingId!, {
        credentials: p.credentials,
        config: p.config,
      });
    },
    onSuccess: (result) => {
      setEditTestResult(result.ok ? "success" : result.detail ?? "failed");
      // F-014-12: reuse the existing connections.testFailed key instead of a
      // hardcoded English "Test failed:" string so the edit-test path is
      // localised like the create/saved-connection handlers.
      setGlobalMessage(
        result.ok
          ? t("connections.testPassed")
          : t("connections.testFailed", { error: result.detail ?? "failed" }),
        result.ok ? "success" : "error",
      );
    },
    onError: (error: unknown) => {
      setEditTestResult(error instanceof Error ? error.message : "failed");
      setGlobalMessage(
        t("connections.testFailed", { error: error instanceof Error ? error.message : "" }),
        "error",
      );
    },
  });

  return (
    <Box>
      <Box display="flex" mb={1.5}>
        <Typography variant="subtitle2" fontWeight={700} flexGrow={1}>
          {t("connections.title")}
        </Typography>
        {canManageConnections && (
          <Button
            size="small"
            variant="contained"
            startIcon={<AddIcon />}
            onClick={() => {
              resetForm("postgresql");
              setOpen(true);
            }}
          >
            {t("connections.add")}
          </Button>
        )}
      </Box>

      {connections.isLoading ? (
        <CircularProgress size={20} />
      ) : (
        <List dense disablePadding>
          {connections.data?.map((c) => (
            <ListItem
              key={c.id}
              sx={{ py: 0.5 }}
              secondaryAction={
                canManageConnections ? (
                  <Box>
                    <Tooltip title={t("connections.edit")}>
                      <IconButton size="small" onClick={() => openEdit(c)}>
                        <EditIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title={t("connections.test")}>
                      <IconButton size="small" onClick={() => testSaved.mutate(c.id)}>
                        <PlayArrowIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title={t("common.delete")}>
                      <IconButton size="small" onClick={() => handleDeleteConn(c)}>
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  </Box>
                ) : null
              }
            >
              <CableIcon fontSize="small" sx={{ mr: 0.5, color: "text.secondary" }} />
              <ListItemText
                primary={c.display_name}
                secondary={
                  savedTestResult[c.id]
                    ? savedTestResult[c.id] === "success"
                      ? t("connections.testStatusPassed", { type: connTypeLabel(c.connection_type) })
                      : t("connections.testStatusFailed", { type: connTypeLabel(c.connection_type), result: savedTestResult[c.id] })
                    : connTypeLabel(c.connection_type)
                }
                primaryTypographyProps={{ variant: "body2" }}
                secondaryTypographyProps={{
                  variant: "caption",
                  color:
                    savedTestResult[c.id] && savedTestResult[c.id] !== "success"
                      ? "error.main"
                      : "text.secondary",
                }}
              />
            </ListItem>
          ))}
          {connections.data?.length === 0 && (
            <Typography variant="body2" color="text.secondary">
              {t("connections.none")}
            </Typography>
          )}
        </List>
      )}

      {canManageConnections && (
        <>
      <ConnectionDialog
        open={open}
        mode="create"
        name={name}
        onNameChange={setName}
        connType={connType}
        onConnTypeChange={(t) => { resetForm(t); setConnType(t); }}
        fields={fields}
        onFieldChange={(k, v) => setFields((prev) => ({ ...prev, [k]: v }))}
        testResult={draftTestResult}
        isError={create.isError}
        isSaving={create.isPending}
        isTesting={testDraft.isPending}
        onTest={() => testDraft.mutate()}
        onSave={() => create.mutate()}
        onClose={() => setOpen(false)}
      />

      <ConnectionDialog
        open={!!editingId}
        mode="edit"
        name={editName}
        onNameChange={setEditName}
        connType={editType}
        onConnTypeChange={(v) => { setEditType(v); setEditFields({}); }}
        fields={editFields}
        onFieldChange={(k, v) => setEditFields((prev) => ({ ...prev, [k]: v }))}
        testResult={editTestResult}
        isError={updateConn.isError}
        isSaving={updateConn.isPending}
        isTesting={testEdit.isPending}
        onTest={() => testEdit.mutate()}
        onSave={() => updateConn.mutate()}
        onClose={() => setEditingId(null)}
        passwordHint
      />
        </>
      )}

    </Box>
  );
}
