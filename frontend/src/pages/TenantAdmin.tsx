import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { safeLocalGet } from "../utils/safeLocalStorage";
import {
  Alert,
  Badge,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  Drawer,
  FormControl,
  IconButton,
  InputLabel,
  Link,
  List,
  ListItemButton,
  ListItemText,
  MenuItem,
  Paper,
  Select,
  Stack,
  Switch,
  Tab,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import AddIcon from "@mui/icons-material/Add";
import ChatIcon from "@mui/icons-material/ChatOutlined";
import HistoryIcon from "@mui/icons-material/HistoryOutlined";
import DeleteIcon from "@mui/icons-material/DeleteOutline";
import DownloadIcon from "@mui/icons-material/FileDownloadOutlined";
import EditIcon from "@mui/icons-material/EditOutlined";
import LockResetIcon from "@mui/icons-material/LockReset";
import PublishIcon from "@mui/icons-material/PublishOutlined";
import SecurityIcon from "@mui/icons-material/SecurityOutlined";
import SettingsIcon from "@mui/icons-material/SettingsOutlined";
import KeyIcon from "@mui/icons-material/VpnKeyOutlined";
import WebhookIcon from "@mui/icons-material/WebhookOutlined";
import UnpublishedIcon from "@mui/icons-material/UnpublishedOutlined";
import UploadIcon from "@mui/icons-material/FileUploadOutlined";
import ProjectConfigDrawer from "../components/Settings/ProjectConfigDrawer";
import EffectiveAccessPreview from "../components/Settings/EffectiveAccessPreview";
import GitSettingsPanel from "../components/Admin/GitSettingsPanel";
import SsoSettingsPanel from "../components/Admin/SsoSettingsPanel";
import EmbedTokensPanel from "../components/Admin/EmbedTokensPanel";
import SecurityAuditPanel from "../components/Admin/SecurityAuditPanel";
import { useConfirm } from "../components/Confirm";
import { grantAccessWithSupersede } from "../components/Admin/grantAccessWithSupersede";
import HelpIconButton from "../components/HelpIconButton";
import { useT } from "../i18n";
import {
  accessApi,
  authApi,
  modelsApi,
  projectsApi,
  webhooksApi,
} from "../api/client";
import api from "../api/client";
import { meetsPasswordPolicy, showsPasswordPolicyError } from "../auth/passwordPolicy";
import { agentApi } from "../api/agentApi";
import {
  parseJoinPopulationBlockedError,
  type JoinPopulationBlockedDetail,
} from "../api/versionsApi";
import JoinPopulationBlockedNotice from "../components/Deploy/JoinPopulationBlockedNotice";
import { extractApiError } from "../utils/extractApiError";
import type {
  AccessRole,
  LocalUserRole,
  Model,
  Project,
  User,
  UserAccessBinding,
} from "../api/types";

type DrawerKind =
  | { kind: "project"; project?: Project }
  | { kind: "model"; project: Project; model?: Model }
  | { kind: "user"; user?: User }
  | { kind: "grant"; project: Project }
  | { kind: "reset-password"; user: User }
  | null;

type DetailTab = "models" | "users-access" | "security-audit" | "git" | "sso" | "embed-tokens";

const ACCESS_ROLES: AccessRole[] = ["admin", "modeler", "viewer", "model_viewer"];
const USER_ROLES: LocalUserRole[] = ["member", "tenant_admin", "model_technical"];

export default function TenantAdmin() {
  const t = useT();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
  const [detailTab, setDetailTab] = useState<DetailTab>("models");
  const [drawer, setDrawer] = useState<DrawerKind>(null);
  const [configProject, setConfigProject] = useState<Project | null>(null);
  const [error, setError] = useState<string | null>(null);

  const tenantId = safeLocalGet("tenant_id", "");

  const projects = useQuery({ queryKey: ["projects"], queryFn: projectsApi.list });
  const dlqCount = useQuery({
    queryKey: ["webhooks-dlq-count"],
    queryFn: webhooksApi.dlqCount,
    refetchInterval: 60_000,
  });
  const users = useQuery({
    queryKey: ["tenant-users"],
    queryFn: () => authApi.listTenantUsers(tenantId),
  });
  const models = useQuery({
    queryKey: ["models", selectedProjectId],
    queryFn: () => modelsApi.list(selectedProjectId!),
    enabled: Boolean(selectedProjectId),
  });
  const access = useQuery({
    queryKey: ["access", selectedProjectId],
    queryFn: () => accessApi.list(selectedProjectId!),
    enabled: Boolean(selectedProjectId),
  });
  const agentConfigQuery = useQuery({
    queryKey: ["agent-config", selectedProjectId],
    queryFn: () => agentApi.getConfig(selectedProjectId!),
    enabled: Boolean(selectedProjectId),
  });

  const usersById = useMemo(() => {
    const map = new Map<string, User>();
    (users.data ?? []).forEach((u) => map.set(u.email, u));
    return map;
  }, [users.data]);

  const selectedProject = useMemo(
    () => (projects.data ?? []).find((p) => p.id === selectedProjectId) ?? null,
    [projects.data, selectedProjectId],
  );

  function close() {
    setDrawer(null);
    setError(null);
  }

  function refreshAll() {
    qc.invalidateQueries({ queryKey: ["projects"] });
    qc.invalidateQueries({ queryKey: ["tenant-users"] });
    if (selectedProjectId) {
      qc.invalidateQueries({ queryKey: ["models", selectedProjectId] });
      qc.invalidateQueries({ queryKey: ["access", selectedProjectId] });
    }
  }

  return (
    <Box sx={{ display: "flex", flex: 1, minHeight: 0, bgcolor: "background.default" }}>
      <Box sx={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <Box
          sx={{
            px: 2,
            pt: 2,
            pb: 1,
            borderBottom: 1,
            borderColor: "divider",
            bgcolor: "background.paper",
          }}
        >
          <Typography variant="h6" sx={{ fontWeight: 700, flex: 1 }}>
            {t("tenantAdmin.title")}
          </Typography>
          <Stack direction="row" spacing={1} alignItems="center">
            <HelpIconButton href="/help/admin/manage-users.html" />
            <Button
              size="small"
              startIcon={<SecurityIcon />}
              component="a"
              href="/admin/audit-log"
              variant="outlined"
            >
              {t("tenantAdmin.auditLogButton")}
            </Button>
            <Button
              size="small"
              startIcon={<KeyIcon />}
              component="a"
              href="/admin/group-mappings"
              variant="outlined"
            >
              {t("tenantAdmin.ssoGroupMappingsButton")}
            </Button>
            <Badge
              badgeContent={dlqCount.data?.count ?? 0}
              color="error"
              invisible={!dlqCount.data?.count}
            >
              <Button
                size="small"
                startIcon={<WebhookIcon />}
                component="a"
                href="/admin/webhooks"
                variant="outlined"
              >
                {t("tenantAdmin.webhooksButton")}
              </Button>
            </Badge>
          </Stack>
        </Box>

        <Box sx={{ flex: 1, display: "flex", minHeight: 0 }}>
          <ProjectRail
            projects={projects.data ?? []}
            loading={projects.isLoading}
            selectedId={selectedProjectId}
            onSelect={setSelectedProjectId}
            onCreate={() => setDrawer({ kind: "project" })}
            onEdit={(p) => setDrawer({ kind: "project", project: p })}
            onConfig={(p) => setConfigProject(p)}
            onDelete={async (p) => {
              const ok = await confirm({
                mode: "typed-name",
                title: t("tenantAdmin.deleteProjectTitle"),
                message: t("tenantAdmin.deleteProjectMessage"),
                confirmText: p.slug,
                confirmLabel: t("tenantAdmin.deleteProjectConfirmLabel"),
              });
              if (!ok) return;
              await projectsApi.delete(p.id);
              if (selectedProjectId === p.id) setSelectedProjectId(null);
              refreshAll();
            }}
          />
          <Divider orientation="vertical" flexItem />
          <Box sx={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
            {selectedProject ? (
              <>
                <Box
                  sx={{
                    px: 2,
                    pt: 1.5,
                    pb: 0,
                    display: "flex",
                    alignItems: "center",
                    gap: 1,
                  }}
                >
                  <Box sx={{ flex: 1 }}>
                    <Typography variant="h6">{selectedProject.display_name}</Typography>
                    <Typography variant="caption" color="text.secondary">
                      {selectedProject.slug}
                    </Typography>
                  </Box>
                  <Tooltip title={t("tenantAdmin.openAgentChat")}>
                    <IconButton
                      size="small"
                      component="a"
                      href={`/tenants/${tenantId}/projects/${selectedProject.id}/agent`}
                    >
                      <ChatIcon />
                    </IconButton>
                  </Tooltip>
                  {agentConfigQuery.data?.enable_agent_log_screen && (
                    <Tooltip title={t("tenantAdmin.agentChatLog")}>
                      <IconButton
                        size="small"
                        component="a"
                        href={`/tenants/${tenantId}/projects/${selectedProject.id}/agent-log`}
                        target="_blank"
                      >
                        <HistoryIcon />
                      </IconButton>
                    </Tooltip>
                  )}
                </Box>
                <Tabs
                  value={detailTab}
                  onChange={(_, v) => setDetailTab(v)}
                  sx={{
                    px: 2,
                    minHeight: 36,
                    "& .MuiTab-root": { minHeight: 36, textTransform: "none" },
                  }}
                >
                  <Tab value="models" label={t("tenantAdmin.tabModels")} />
                  <Tab value="users-access" label={t("tenantAdmin.tabUsersAccess")} />
                  <Tab value="security-audit" label={t("tenantAdmin.tabSecurityAudit")} />
                  <Tab value="sso" label={t("tenantAdmin.tabSso")} />
                  <Tab value="embed-tokens" label={t("tenantAdmin.tabEmbedTokens")} />
                  <Tab value="git" label={t("git.sectionTitle")} />
                </Tabs>
                <Divider />
                <Box sx={{ flex: 1, overflow: "auto", p: 2 }}>
                  {detailTab === "models" ? (
                    <ModelsSection
                      project={selectedProject}
                      models={models.data ?? []}
                      loading={models.isLoading}
                      tenantId={tenantId}
                      onCreate={() =>
                        setDrawer({ kind: "model", project: selectedProject })
                      }
                      onEdit={(m) =>
                        setDrawer({ kind: "model", project: selectedProject, model: m })
                      }
                      onDelete={async (m) => {
                        const ok = await confirm({
                          mode: "typed-name",
                          title: t("tenantAdmin.deleteModelTitle"),
                          message: t("tenantAdmin.deleteModelMessage"),
                          confirmText: m.slug,
                          confirmLabel: t("tenantAdmin.deleteModelConfirmLabel"),
                        });
                        if (!ok) return;
                        await modelsApi.delete(selectedProject.id, m.id);
                        refreshAll();
                      }}
                      onRefresh={refreshAll}
                    />
                  ) : detailTab === "security-audit" ? (
                    <SecurityAuditPanel />
                  ) : detailTab === "sso" ? (
                    <SsoSettingsPanel />
                  ) : detailTab === "embed-tokens" ? (
                    <EmbedTokensPanel />
                  ) : detailTab === "git" ? (
                    <GitSettingsPanel />
                  ) : (
                    <UsersAccessSection
                      project={selectedProject}
                      users={users.data ?? []}
                      usersLoading={users.isLoading}
                      access={access.data ?? []}
                      accessLoading={access.isLoading}
                      usersById={usersById}
                      onCreateUser={() => setDrawer({ kind: "user" })}
                      onEditUser={(u) => setDrawer({ kind: "user", user: u })}
                      onResetPassword={(u) =>
                        setDrawer({ kind: "reset-password", user: u })
                      }
                      onDeleteUser={async (u) => {
                        const ok = await confirm({
                          mode: "typed-name",
                          title: t("tenantAdmin.deleteUserTitle"),
                          message: t("tenantAdmin.deleteUserMessage"),
                          confirmText: u.email,
                          confirmLabel: t("tenantAdmin.deleteUserConfirmLabel"),
                        });
                        if (!ok) return;
                        await authApi.deleteTenantUser(tenantId, u.id);
                        refreshAll();
                      }}
                      onGrant={() =>
                        setDrawer({ kind: "grant", project: selectedProject })
                      }
                      onRevoke={async (b) => {
                        const ok = await confirm({
                          title: t("tenantAdmin.revokeAccessTitle"),
                          message: t("tenantAdmin.revokeAccessMessage", { scope: b.model_id ? "model" : "project" }),
                          confirmLabel: t("tenantAdmin.revokeAccessConfirmLabel"),
                        });
                        if (!ok) return;
                        await accessApi.revoke(selectedProject.id, b.id);
                        refreshAll();
                      }}
                    />
                  )}
                </Box>
              </>
            ) : (
              <Box sx={{ p: 3 }}>
                <Typography variant="body2" color="text.secondary">
                  {t("tenantAdmin.selectProject")}
                </Typography>
              </Box>
            )}
          </Box>
        </Box>
      </Box>

      <EditDrawer
        drawer={drawer}
        users={users.data ?? []}
        models={models.data ?? []}
        error={error}
        setError={setError}
        onClose={close}
        onSaved={() => {
          close();
          refreshAll();
        }}
      />

      <ProjectConfigDrawer
        project={configProject}
        open={configProject !== null}
        onClose={() => {
          setConfigProject(null);
          if (selectedProjectId) {
            qc.invalidateQueries({ queryKey: ["agent-config", selectedProjectId] });
          }
        }}
      />
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Project rail (left column)
// ---------------------------------------------------------------------------

function ProjectRail({
  projects,
  loading,
  selectedId,
  onSelect,
  onCreate,
  onEdit,
  onConfig,
  onDelete,
}: {
  projects: Project[];
  loading: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onEdit: (p: Project) => void;
  onConfig: (p: Project) => void;
  onDelete: (p: Project) => void;
}) {
  const t = useT();
  return (
    <Box sx={{ width: 260, display: "flex", flexDirection: "column" }}>
      <Box sx={{ px: 1.5, py: 1, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <Typography variant="overline" color="text.secondary">
          {t("tenantAdmin.projectsHeader")}
        </Typography>
        <Tooltip title={t("tenantAdmin.newProjectTooltip")}>
          <IconButton size="small" onClick={onCreate}>
            <AddIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      </Box>
      <Divider />
      {loading ? (
        <Box sx={{ p: 2 }}>
          <CircularProgress size={18} />
        </Box>
      ) : (
        <List dense disablePadding sx={{ overflow: "auto" }}>
          {projects.map((p) => (
            <ListItemButton
              key={p.id}
              selected={p.id === selectedId}
              onClick={() => onSelect(p.id)}
              sx={{
                py: 0.5,
                "&:hover .row-actions": { opacity: 1 },
              }}
            >
              <ListItemText
                primary={p.display_name}
                secondary={p.slug}
                primaryTypographyProps={{ variant: "body2", noWrap: true }}
                secondaryTypographyProps={{ variant: "caption" }}
              />
              <Tooltip title={t("tenantAdmin.configureProject")}>
                <IconButton
                  size="small"
                  onClick={(e) => {
                    e.stopPropagation();
                    onConfig(p);
                  }}
                >
                  <SettingsIcon sx={{ fontSize: 18, color: "action.active" }} />
                </IconButton>
              </Tooltip>
              <Box className="row-actions" sx={{ opacity: 0, display: "flex", gap: 0.25 }}>
                <IconButton size="small" onClick={(e) => { e.stopPropagation(); onEdit(p); }}>
                  <EditIcon sx={{ fontSize: 16 }} />
                </IconButton>
                <IconButton size="small" onClick={(e) => { e.stopPropagation(); onDelete(p); }}>
                  <DeleteIcon sx={{ fontSize: 16 }} />
                </IconButton>
              </Box>
            </ListItemButton>
          ))}
        </List>
      )}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Models section (Models tab)
// ---------------------------------------------------------------------------

function ModelsSection({
  project,
  models,
  loading,
  tenantId,
  onCreate,
  onEdit,
  onDelete,
  onRefresh,
}: {
  project: Project;
  models: Model[];
  loading: boolean;
  tenantId: string;
  onCreate: () => void;
  onEdit: (m: Model) => void;
  onDelete: (m: Model) => void;
  onRefresh: () => void;
}) {
  const t = useT();
  const confirm = useConfirm();
  const navigate = useNavigate();
  const [deployRefusal, setDeployRefusal] = useState<{
    modelId: string;
    detail: JoinPopulationBlockedDetail;
  } | null>(null);
  const [deployError, setDeployError] = useState<string | null>(null);

  const deployMut = useMutation({
    mutationFn: async (model: Model) => {
      await api.post(
        `/api/v1/projects/${project.id}/models/${model.id}/deploy`,
      );
    },
    onSuccess: () => {
      setDeployRefusal(null);
      setDeployError(null);
      onRefresh();
    },
    onError: (error: unknown, model: Model) => {
      const detail = parseJoinPopulationBlockedError(error);
      if (detail) {
        setDeployError(null);
        setDeployRefusal({ modelId: model.id, detail });
      } else {
        setDeployRefusal(null);
        setDeployError(extractApiError(error, t("errors.requestFailed")));
      }
    },
  });

  const undeployMut = useMutation({
    mutationFn: async (model: Model) => {
      await api.post(
        `/api/v1/projects/${project.id}/models/${model.id}/undeploy`,
      );
    },
    onSuccess: onRefresh,
  });

  async function handleExport(model: Model) {
    const data = await modelsApi.export(project.id, model.id);
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${model.slug}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const importMut = useMutation({
    mutationFn: async (file: File) => {
      const text = await file.text();
      const payload = JSON.parse(text);
      await api.post(
        `/api/v1/projects/${project.id}/models/snapshot-import`,
        payload,
      );
    },
    onSuccess: onRefresh,
  });

  function handleImport() {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".json";
    input.onchange = () => {
      const file = input.files?.[0];
      if (file) importMut.mutate(file);
    };
    input.click();
  }

  return (
    <Paper variant="outlined">
      <Box sx={{ px: 1.5, py: 0.75, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <Typography variant="overline" color="text.secondary">{t("tenantAdmin.modelsHeader")}</Typography>
        <Box sx={{ display: "flex", gap: 0.5 }}>
          <Button
            size="small"
            startIcon={<UploadIcon sx={{ fontSize: 16 }} />}
            onClick={handleImport}
            disabled={importMut.isPending}
          >
            {t("tenantAdmin.importButton")}
          </Button>
          <Button
            size="small"
            startIcon={<AddIcon sx={{ fontSize: 16 }} />}
            onClick={onCreate}
          >
            {t("tenantAdmin.newModelButton")}
          </Button>
        </Box>
      </Box>
      <Divider />
      {deployRefusal && (
        <Box sx={{ p: 1.5 }}>
          <JoinPopulationBlockedNotice
            detail={deployRefusal.detail}
            onClose={() => setDeployRefusal(null)}
            onOpenJoins={() => {
              const modelId = deployRefusal.modelId;
              setDeployRefusal(null);
              navigate(
                `/tenants/${tenantId}/projects/${project.id}/models/${modelId}?panel=joins`,
              );
            }}
          />
        </Box>
      )}
      {deployError && (
        <Alert severity="error" sx={{ m: 1.5 }} onClose={() => setDeployError(null)}>
          {deployError}
        </Alert>
      )}
      {loading ? (
        <Box sx={{ p: 2 }}><CircularProgress size={18} /></Box>
      ) : models.length === 0 ? (
        <Box sx={{ p: 2 }}>
          <Typography variant="caption" color="text.secondary">
            {t("tenantAdmin.noModels")}
          </Typography>
        </Box>
      ) : (
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>{t("tenantAdmin.colName")}</TableCell>
              <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>{t("tenantAdmin.colSlug")}</TableCell>
              <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>{t("tenantAdmin.colStatus")}</TableCell>
              <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>{t("tenantAdmin.colLastDeployed")}</TableCell>
              <TableCell sx={{ fontSize: 12, color: "text.secondary" }} align="right">{t("tenantAdmin.colActions")}</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {models.map((m) => {
              const isDeployed = Boolean(m.deployed_version_id);
              return (
                <TableRow key={m.id} sx={{ "&:hover .row-actions": { opacity: 1 } }}>
                  <TableCell sx={{ fontSize: 13, py: 0.5 }}>
                    <Link
                      href={`/tenants/${tenantId}/projects/${project.id}/models/${m.id}`}
                      underline="hover"
                      color="inherit"
                      sx={{ fontWeight: 500 }}
                    >
                      {m.display_name}
                    </Link>
                  </TableCell>
                  <TableCell sx={{ fontSize: 13, py: 0.5 }}>{m.slug}</TableCell>
                  <TableCell sx={{ fontSize: 13, py: 0.5 }}>
                    <Chip
                      size="small"
                      label={isDeployed ? t("tenantAdmin.statusDeployed") : t("tenantAdmin.statusDraft")}
                      color={isDeployed ? "success" : "default"}
                      variant={isDeployed ? "filled" : "outlined"}
                      sx={{ height: 20, fontSize: "0.65rem" }}
                    />
                  </TableCell>
                  <TableCell sx={{ fontSize: 13, py: 0.5 }}>
                    {m.last_deployed_at
                      ? new Date(m.last_deployed_at).toLocaleString()
                      : t("common.na")}
                  </TableCell>
                  <TableCell align="right" sx={{ py: 0.5, whiteSpace: "nowrap" }}>
                    {isDeployed ? (
                      <Tooltip title={t("tenantAdmin.undeployTooltip")}>
                        <IconButton
                          size="small"
                          onClick={() => undeployMut.mutate(m)}
                          disabled={undeployMut.isPending}
                          aria-label={t("tenantAdmin.undeployTooltip")}
                        >
                          <UnpublishedIcon sx={{ fontSize: 18 }} />
                        </IconButton>
                      </Tooltip>
                    ) : (
                      <Tooltip title={t("tenantAdmin.deployTooltip")}>
                        <IconButton
                          size="small"
                          onClick={() => deployMut.mutate(m)}
                          disabled={deployMut.isPending}
                          aria-label={t("tenantAdmin.deployTooltip")}
                        >
                          <PublishIcon sx={{ fontSize: 18 }} />
                        </IconButton>
                      </Tooltip>
                    )}
                    <Tooltip title={t("tenantAdmin.exportModelTooltip")}>
                      <IconButton size="small" onClick={() => handleExport(m)}>
                        <DownloadIcon sx={{ fontSize: 18 }} />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title={t("tenantAdmin.editModelTooltip")}>
                      <IconButton size="small" onClick={() => onEdit(m)}>
                        <EditIcon sx={{ fontSize: 16 }} />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title={t("tenantAdmin.deleteModelTooltip")}>
                      <IconButton size="small" onClick={() => onDelete(m)}>
                        <DeleteIcon sx={{ fontSize: 16 }} />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
    </Paper>
  );
}

// ---------------------------------------------------------------------------
// Users & Access section (Users & Access tab)
// ---------------------------------------------------------------------------

function UsersAccessSection({
  project,
  users,
  usersLoading,
  access,
  accessLoading,
  usersById,
  onCreateUser,
  onEditUser,
  onResetPassword,
  onDeleteUser,
  onGrant,
  onRevoke,
}: {
  project: Project;
  users: User[];
  usersLoading: boolean;
  access: UserAccessBinding[];
  accessLoading: boolean;
  usersById: Map<string, User>;
  onCreateUser: () => void;
  onEditUser: (u: User) => void;
  onResetPassword: (u: User) => void;
  onDeleteUser: (u: User) => void;
  onGrant: () => void;
  onRevoke: (b: UserAccessBinding) => void;
}) {
  const t = useT();
  return (
    <Stack spacing={2}>
      <Paper variant="outlined">
        <SectionHeader title={t("tenantAdmin.tenantUsersHeader")} actionLabel={t("tenantAdmin.newUserButton")} onAction={onCreateUser} />
        <Divider />
        {usersLoading ? (
          <Box sx={{ p: 2 }}><CircularProgress size={18} /></Box>
        ) : users.length === 0 ? (
          <Box sx={{ p: 2 }}>
            <Typography variant="caption" color="text.secondary">
              {t("tenantAdmin.noUsers")}
            </Typography>
          </Box>
        ) : (
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>{t("tenantAdmin.colEmail")}</TableCell>
                <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>{t("tenantAdmin.colUsername")}</TableCell>
                <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>{t("tenantAdmin.colRole")}</TableCell>
                <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>{t("tenantAdmin.colActive")}</TableCell>
                <TableCell sx={{ fontSize: 12, color: "text.secondary" }} align="right" />
              </TableRow>
            </TableHead>
            <TableBody>
              {users.map((u) => (
                <TableRow key={u.id}>
                  <TableCell sx={{ fontSize: 13, py: 0.5 }}>{u.email}</TableCell>
                  <TableCell sx={{ fontSize: 13, py: 0.5 }}>{u.username}</TableCell>
                  <TableCell sx={{ fontSize: 13, py: 0.5 }}>{t(`roles.${u.role}`)}</TableCell>
                  <TableCell sx={{ fontSize: 13, py: 0.5 }}>{u.is_active ? t("tenantAdmin.activeYes") : t("tenantAdmin.activeNo")}</TableCell>
                  <TableCell align="right" sx={{ py: 0.5, whiteSpace: "nowrap" }}>
                    <Tooltip title={t("tenantAdmin.editUserTooltip")}>
                      <IconButton size="small" onClick={() => onEditUser(u)}>
                        <EditIcon sx={{ fontSize: 16 }} />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title={t("tenantAdmin.resetPasswordTooltip")}>
                      <IconButton size="small" onClick={() => onResetPassword(u)}>
                        <LockResetIcon sx={{ fontSize: 16 }} />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title={t("tenantAdmin.deleteUserTooltip")}>
                      <IconButton size="small" onClick={() => onDeleteUser(u)}>
                        <DeleteIcon sx={{ fontSize: 16 }} />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Paper>

      <Paper variant="outlined">
        <SectionHeader
          title={t("tenantAdmin.accessHeader", { project: project.display_name })}
          actionLabel={t("tenantAdmin.grantAccessButton")}
          onAction={onGrant}
        />
        <Divider />
        {accessLoading ? (
          <Box sx={{ p: 2 }}><CircularProgress size={18} /></Box>
        ) : access.length === 0 ? (
          <Box sx={{ p: 2 }}>
            <Typography variant="caption" color="text.secondary">
              {t("tenantAdmin.noAccess")}
            </Typography>
          </Box>
        ) : (
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>{t("tenantAdmin.colUser")}</TableCell>
                <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>{t("tenantAdmin.colRole")}</TableCell>
                <TableCell sx={{ fontSize: 12, color: "text.secondary" }}>{t("tenantAdmin.colScope")}</TableCell>
                <TableCell sx={{ fontSize: 12, color: "text.secondary" }} align="right" />
              </TableRow>
            </TableHead>
            <TableBody>
              {access.map((b) => (
                <TableRow key={b.id}>
                  <TableCell sx={{ fontSize: 13, py: 0.5 }}>
                    {usersById.get(b.user_identity)?.email ?? b.user_identity}
                  </TableCell>
                  <TableCell sx={{ fontSize: 13, py: 0.5 }}>{t(`roles.${b.role}`)}</TableCell>
                  <TableCell sx={{ fontSize: 13, py: 0.5 }}>
                    {b.model_id ? t("tenantAdmin.scopeModelLevel") : t("tenantAdmin.scopeProjectWide")}
                  </TableCell>
                  <TableCell align="right" sx={{ py: 0.5 }}>
                    <Tooltip title={t("tenantAdmin.revokeTooltip")}>
                      <IconButton size="small" onClick={() => onRevoke(b)}>
                        <DeleteIcon sx={{ fontSize: 16 }} />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Paper>
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Right-side drawer for create/edit forms
// ---------------------------------------------------------------------------

function EditDrawer({
  drawer,
  users,
  models,
  error,
  setError,
  onClose,
  onSaved,
}: {
  drawer: DrawerKind;
  users: User[];
  models: Model[];
  error: string | null;
  setError: (e: string | null) => void;
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useT();
  const confirm = useConfirm();
  const [projSlug, setProjSlug] = useState("");
  const [projName, setProjName] = useState("");
  const [modelSlug, setModelSlug] = useState("");
  const [modelName, setModelName] = useState("");
  const [modelActive, setModelActive] = useState(true);
  const [email, setEmail] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<LocalUserRole>("member");
  const [grantUser, setGrantUser] = useState("");
  const [grantRole, setGrantRole] = useState<AccessRole>("viewer");
  const [grantModelId, setGrantModelId] = useState<string>("");

  useEffect(() => {
    setError(null);
    if (drawer?.kind === "project") {
      setProjSlug(drawer.project?.slug ?? "");
      setProjName(drawer.project?.display_name ?? "");
    } else if (drawer?.kind === "model") {
      setModelSlug(drawer.model?.slug ?? "");
      setModelName(drawer.model?.display_name ?? "");
      setModelActive(drawer.model ? drawer.model.status !== "disabled" : true);
    } else if (drawer?.kind === "user") {
      setEmail(drawer.user?.email ?? "");
      setUsername(drawer.user?.username ?? "");
      setPassword("");
      setRole((drawer.user?.role as LocalUserRole) ?? "member");
    } else if (drawer?.kind === "grant") {
      setGrantUser("");
      setGrantRole("viewer");
      setGrantModelId("");
    } else if (drawer?.kind === "reset-password") {
      setPassword("");
    }
  }, [drawer, setError]);

  // Bug-8184: same two endpoints as the users & access panel
  // (createTenantUser / resetTenantUserPassword), so the same rule is stated
  // here rather than left to a 422.
  const passwordOk = meetsPasswordPolicy(password);
  const passwordInvalid = showsPasswordPolicyError(password);

  const formValid = (() => {
    if (drawer?.kind === "project") return projSlug.trim() !== "" && projName.trim() !== "";
    if (drawer?.kind === "model") return modelSlug.trim() !== "" && modelName.trim() !== "";
    if (drawer?.kind === "user") {
      const baseValid = email.trim() !== "" && username.trim() !== "";
      return drawer.user ? baseValid : baseValid && passwordOk;
    }
    if (drawer?.kind === "grant") return grantUser !== "";
    if (drawer?.kind === "reset-password") return passwordOk;
    return false;
  })();

  const save = useMutation({
    mutationFn: async (): Promise<"saved" | "cancelled"> => {
      const tenantId = safeLocalGet("tenant_id", "");
      if (drawer?.kind === "project") {
        if (drawer.project) {
          await projectsApi.update(drawer.project.id, {
            slug: projSlug,
            display_name: projName,
          });
        } else {
          await projectsApi.create({ slug: projSlug, display_name: projName });
        }
      } else if (drawer?.kind === "model") {
        if (drawer.model) {
          await modelsApi.update(drawer.project.id, drawer.model.id, {
            slug: modelSlug,
            display_name: modelName,
            status: modelActive ? "active" : "disabled",
          });
        } else {
          await modelsApi.create(drawer.project.id, {
            slug: modelSlug,
            display_name: modelName,
          });
        }
      } else if (drawer?.kind === "user") {
        if (drawer.user) {
          await authApi.updateTenantUser(tenantId, drawer.user.id, {
            email,
            username,
            role,
          });
        } else {
          await authApi.createTenantUser(tenantId, {
            email,
            username,
            password,
            role,
          });
        }
      } else if (drawer?.kind === "grant") {
        // Bug-8101: Modeller-supersedes-Model-viewer confirmation before grant.
        // On cancel nothing changes and the drawer stays open.
        const outcome = await grantAccessWithSupersede(
          drawer.project.id,
          {
            user_identity: grantUser,
            role: grantRole,
            model_id: grantModelId === "" ? null : grantModelId,
          },
          confirm,
          {
            title: t("tenantAdmin.supersedeTitle"),
            message: t("tenantAdmin.supersedeMessage"),
            confirmLabel: t("tenantAdmin.supersedeConfirm"),
          },
        );
        if (outcome === "cancelled") return "cancelled";
      } else if (drawer?.kind === "reset-password") {
        await authApi.resetTenantUserPassword(tenantId, drawer.user.id, {
          password,
        });
      }
      return "saved";
    },
    onSuccess: (outcome) => {
      if (outcome === "cancelled") return;
      onSaved();
    },
    onError: (err: unknown) => {
      const detail = (err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail;
      setError(detail ?? t("common.saveFailed"));
    },
  });

  return (
    <Drawer anchor="right" open={drawer !== null} onClose={onClose}>
      <Box sx={{ width: 360, p: 2, display: "flex", flexDirection: "column", height: "100%" }}>
        <Typography variant="h6" sx={{ mb: 2 }}>
          {drawer?.kind === "project"
            ? drawer.project
              ? t("tenantAdmin.drawerEditProject")
              : t("tenantAdmin.drawerNewProject")
            : drawer?.kind === "model"
              ? drawer.model
                ? t("tenantAdmin.drawerEditModel")
                : t("tenantAdmin.drawerNewModel")
              : drawer?.kind === "user"
                ? drawer.user
                  ? t("tenantAdmin.drawerEditUser")
                  : t("tenantAdmin.drawerNewUser")
                : drawer?.kind === "grant"
                  ? t("tenantAdmin.drawerGrantAccess")
                  : drawer?.kind === "reset-password"
                    ? t("tenantAdmin.drawerResetPassword")
                    : ""}
        </Typography>

        {error ? <Alert severity="error" sx={{ mb: 1 }}>{error}</Alert> : null}

        {drawer?.kind === "project" && (
          <Stack spacing={2} sx={{ flex: 1 }}>
            <TextField label={t("tenantAdmin.fieldSlug")} size="small" value={projSlug} onChange={(e) => setProjSlug(e.target.value)} />
            <TextField label={t("tenantAdmin.fieldDisplayName")} size="small" value={projName} onChange={(e) => setProjName(e.target.value)} />
          </Stack>
        )}

        {drawer?.kind === "model" && (
          <Stack spacing={2} sx={{ flex: 1 }}>
            <TextField label={t("tenantAdmin.fieldSlug")} size="small" value={modelSlug} onChange={(e) => setModelSlug(e.target.value)} />
            <TextField label={t("tenantAdmin.fieldDisplayName")} size="small" value={modelName} onChange={(e) => setModelName(e.target.value)} />
            <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
              <Switch size="small" checked={modelActive} onChange={(e) => setModelActive(e.target.checked)} />
              <Typography variant="body2">{t("tenantAdmin.fieldActive")}</Typography>
            </Box>
          </Stack>
        )}

        {drawer?.kind === "user" && (
          <Stack spacing={2} sx={{ flex: 1 }}>
            <TextField label={t("tenantAdmin.fieldEmail")} size="small" value={email} onChange={(e) => setEmail(e.target.value)} />
            <TextField label={t("tenantAdmin.fieldUsername")} size="small" value={username} onChange={(e) => setUsername(e.target.value)} />
            {!drawer.user && (
              <TextField
                label={t("tenantAdmin.fieldPassword")}
                type="password"
                size="small"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                error={passwordInvalid}
                helperText={t("errors.form.passwordComplexity")}
                InputProps={{ startAdornment: <KeyIcon sx={{ fontSize: 16, mr: 1, color: "text.secondary" }} /> }}
              />
            )}
            <FormControl size="small">
              <InputLabel>{t("tenantAdmin.fieldRole")}</InputLabel>
              <Select label={t("tenantAdmin.fieldRole")} value={role} onChange={(e) => setRole(e.target.value as LocalUserRole)}>
                {USER_ROLES.map((r) => (
                  <MenuItem key={r} value={r}>{t(`roles.${r}`)}</MenuItem>
                ))}
              </Select>
            </FormControl>
            <EffectiveAccessPreview role={role} />
          </Stack>
        )}

        {drawer?.kind === "grant" && (
          <Stack spacing={2} sx={{ flex: 1 }}>
            <FormControl size="small">
              <InputLabel>{t("tenantAdmin.fieldUser")}</InputLabel>
              <Select label={t("tenantAdmin.fieldUser")} value={grantUser} onChange={(e) => setGrantUser(e.target.value)}>
                {users.map((u) => (
                  <MenuItem key={u.id} value={u.email}>{u.email}</MenuItem>
                ))}
              </Select>
            </FormControl>
            <FormControl size="small">
              <InputLabel>{t("tenantAdmin.fieldRole")}</InputLabel>
              <Select label={t("tenantAdmin.fieldRole")} value={grantRole} onChange={(e) => setGrantRole(e.target.value as AccessRole)}>
                {ACCESS_ROLES.map((r) => (
                  <MenuItem key={r} value={r}>{t(`roles.${r}`)}</MenuItem>
                ))}
              </Select>
            </FormControl>
            <FormControl size="small">
              <InputLabel>{t("tenantAdmin.fieldScope")}</InputLabel>
              <Select
                label={t("tenantAdmin.fieldScope")}
                value={grantModelId}
                onChange={(e) => setGrantModelId(e.target.value)}
              >
                <MenuItem value="">{t("tenantAdmin.scopeProjectWideAll")}</MenuItem>
                {models.map((m) => (
                  <MenuItem key={m.id} value={m.id}>
                    {t("tenantAdmin.scopeModel", { name: m.display_name })}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          </Stack>
        )}

        {drawer?.kind === "reset-password" && (
          <Stack spacing={2} sx={{ flex: 1 }}>
            <Typography variant="body2" color="text.secondary">
              {drawer.user.email}
            </Typography>
            <TextField
              label={t("tenantAdmin.fieldNewPassword")}
              type="password"
              size="small"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              error={passwordInvalid}
              helperText={t("errors.form.passwordComplexity")}
            />
          </Stack>
        )}

        <Box sx={{ display: "flex", justifyContent: "flex-end", gap: 1, mt: 2 }}>
          <Button size="small" onClick={onClose}>{t("tenantAdmin.cancelButton")}</Button>
          <Button
            size="small"
            variant="contained"
            disabled={save.isPending || !formValid}
            onClick={() => save.mutate()}
          >
            {save.isPending ? <CircularProgress size={16} /> : t("tenantAdmin.saveButton")}
          </Button>
        </Box>
      </Box>
    </Drawer>
  );
}

// ---------------------------------------------------------------------------
// Shared components
// ---------------------------------------------------------------------------

function SectionHeader({
  title,
  actionLabel,
  onAction,
}: {
  title: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <Box sx={{ px: 1.5, py: 0.75, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
      <Typography variant="overline" color="text.secondary">{title}</Typography>
      {actionLabel && onAction && (
        <Button size="small" startIcon={<AddIcon sx={{ fontSize: 16 }} />} onClick={onAction}>
          {actionLabel}
        </Button>
      )}
    </Box>
  );
}
