import { useCallback, useEffect, useMemo, useState } from "react";
import { safeLocalGet, safeLocalGetJson } from "../utils/safeLocalStorage";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "../i18n";
import {
  Alert,
  Box,
  Button,
  Card,
  CardActionArea,
  CardContent,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControlLabel,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Paper,
  Switch,
  TextField,
  Tooltip,
  Typography,
  Drawer,
  Toolbar,
  useMediaQuery,
  useTheme,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import BusinessIcon from "@mui/icons-material/Business";
import FolderIcon from "@mui/icons-material/Folder";
import FolderOpenIcon from "@mui/icons-material/FolderOpen";
import StorageIcon from "@mui/icons-material/Storage";
import DeleteIcon from "@mui/icons-material/Delete";
import SettingsIcon from "@mui/icons-material/Settings";
import EditIcon from "@mui/icons-material/Edit";
import MenuIcon from "@mui/icons-material/Menu";
import ChatIcon from "@mui/icons-material/Chat";
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome";
import ImportExportIcon from "@mui/icons-material/ImportExport";
import RocketLaunchIcon from "@mui/icons-material/RocketLaunch";
import DashboardIcon from "@mui/icons-material/Dashboard";
import StarIcon from "@mui/icons-material/Star";
import StarBorderIcon from "@mui/icons-material/StarBorder";
import SearchIcon from "@mui/icons-material/Search";
import InputAdornment from "@mui/material/InputAdornment";
import { useConfirm } from "../components/Confirm";
import ProjectImportExportDialog from "../components/importExport/ProjectImportExportDialog";
import ModelImportExportDialog from "../components/importExport/ModelImportExportDialog";
import ProjectConfigDrawer from "../components/Settings/ProjectConfigDrawer";
import HelpIconButton from "../components/HelpIconButton";
import { canPerform } from "../auth/explorerPrivileges";
import { projectsApi, modelsApi } from "../api/client";
import { agentApi } from "../api/agentApi";
import { versionsApi } from "../api/versionsApi";
import { useModels, useProjects, useTenantMe } from "../api/hooks";
import type { Model, Project } from "../api/types";

const DRAWER_WIDTH = 260;


export default function Explorer() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const t = useT();
  const tenantSlug = safeLocalGet("tenant_id", "");
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down("md"));

  const [mobileOpen, setMobileOpen] = useState(false);
  const [selectedProject, setSelectedProject] = useState<Project | null>(null);

  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [newModelOpen, setNewModelOpen] = useState(false);
  const [projectImportExportOpen, setProjectImportExportOpen] = useState(false);
  const [modelImportExportOpen, setModelImportExportOpen] = useState(false);
  const [formSlug, setFormSlug] = useState("");
  const [formName, setFormName] = useState("");

  const [configProject, setConfigProject] = useState<Project | null>(null);

  const [renameOpen, setRenameOpen] = useState(false);
  const [renameType, setRenameType] = useState<"project" | "model" | null>(null);
  const [renameTargetId, setRenameTargetId] = useState<string>("");
  const [renameValue, setRenameValue] = useState("");

  const [projectFilter, setProjectFilter] = useState("");
  const [pinnedProjects, setPinnedProjects] = useState<string[]>(
    () => safeLocalGetJson(`pinned_projects_${tenantSlug}`, [] as string[]),
  );
  const [pinnedModels, setPinnedModels] = useState<string[]>(
    () => safeLocalGetJson(`pinned_models_${tenantSlug}`, [] as string[]),
  );

  const togglePinProject = useCallback((id: string) => {
    setPinnedProjects((prev) => {
      const next = prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id];
      localStorage.setItem(`pinned_projects_${tenantSlug}`, JSON.stringify(next));
      return next;
    });
  }, [tenantSlug]);

  const togglePinModel = useCallback((id: string) => {
    setPinnedModels((prev) => {
      const next = prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id];
      localStorage.setItem(`pinned_models_${tenantSlug}`, JSON.stringify(next));
      return next;
    });
  }, [tenantSlug]);

  const tenant = useTenantMe();
  const projects = useProjects();
  const models = useModels(selectedProject?.id ?? "");
  const agentConfigQuery = useQuery({
    queryKey: ["agent-config", selectedProject?.id],
    queryFn: () => agentApi.getConfig(selectedProject!.id),
    enabled: Boolean(selectedProject?.id),
  });

  const sortedProjects = useMemo(() => {
    if (!projects.data) return [];
    const filtered = projectFilter
      ? projects.data.filter((p) =>
          p.display_name.toLowerCase().includes(projectFilter.toLowerCase()),
        )
      : projects.data;
    return [...filtered].sort((a, b) => {
      const ap = pinnedProjects.includes(a.id) ? 0 : 1;
      const bp = pinnedProjects.includes(b.id) ? 0 : 1;
      return ap - bp;
    });
  }, [projects.data, projectFilter, pinnedProjects]);

  const sortedModels = useMemo(() => {
    if (!models.data) return [];
    return [...models.data].sort((a, b) => {
      const ap = pinnedModels.includes(a.id) ? 0 : 1;
      const bp = pinnedModels.includes(b.id) ? 0 : 1;
      return ap - bp;
    });
  }, [models.data, pinnedModels]);

  useEffect(() => {
    if (!projects.data || projects.data.length === 0) return;
    if (!selectedProject) {
      setSelectedProject(projects.data[0]);
      return;
    }
    const refreshed = projects.data.find((p) => p.id === selectedProject.id);
    if (refreshed && refreshed !== selectedProject) {
      setSelectedProject(refreshed);
    }
  }, [projects.data, selectedProject]);

  // ── mutations ────────────────────────────────────────────────────

  const createProject = useMutation({
    mutationFn: () =>
      projectsApi.create({ slug: formSlug, display_name: formName || undefined }),
    onSuccess: (newProject: Project) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      setNewProjectOpen(false);
      setSelectedProject(newProject);
    },
  });

  const createModel = useMutation({
    mutationFn: () =>
      modelsApi.create(selectedProject!.id, {
        slug: formSlug,
        display_name: formName || undefined,
      }),
    onSuccess: (model: Model) => {
      qc.invalidateQueries({ queryKey: ["models", selectedProject!.id] });
      setNewModelOpen(false);
      navigate(
        `/tenants/${tenantSlug}/projects/${selectedProject!.id}/models/${model.id}`,
      );
    },
  });

  const toggleProjectActive = useMutation({
    mutationFn: (params: { projectId: string; enabled: boolean }) =>
      projectsApi.update(params.projectId, { is_active: params.enabled }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  const updateProject = useMutation({
    mutationFn: (params: { projectId: string; display_name: string }) =>
      projectsApi.update(params.projectId, { display_name: params.display_name }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      setRenameOpen(false);
    },
  });

  const deleteProject = useMutation({
    mutationFn: async (projectId: string) => {
      await projectsApi.delete(projectId);
      return projectId;
    },
    onSuccess: (deletedProjectId: string) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      if (selectedProject?.id === deletedProjectId) {
        setSelectedProject(null);
      }
    },
  });

  const deployModel = useMutation({
    mutationFn: (modelId: string) =>
      versionsApi.deploy(selectedProject!.id, modelId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models", selectedProject!.id] });
    },
  });

  const undeployModel = useMutation({
    mutationFn: (modelId: string) =>
      versionsApi.undeploy(selectedProject!.id, modelId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models", selectedProject!.id] });
    },
  });

  const updateModel = useMutation({
    mutationFn: (params: { modelId: string; display_name: string }) =>
      modelsApi.update(selectedProject!.id, params.modelId, {
        display_name: params.display_name,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models", selectedProject!.id] });
      setRenameOpen(false);
    },
  });

  const deleteModel = useMutation({
    mutationFn: async (modelId: string) => {
      await modelsApi.delete(selectedProject!.id, modelId);
      return modelId;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models", selectedProject!.id] });
    },
  });

  // ── handlers ─────────────────────────────────────────────────────

  function openDialog(which: "project" | "model") {
    setFormSlug("");
    setFormName("");
    if (which === "project") setNewProjectOpen(true);
    else setNewModelOpen(true);
  }

  function handleModelClick(model: Model) {
    navigate(
      `/tenants/${tenantSlug}/projects/${selectedProject!.id}/models/${model.id}`,
    );
  }

  async function handleDeleteProject(projectId: string, projectSlug: string) {
    const ok = await confirm({
      mode: "typed-name",
      title: t("explorer.deleteProjectConfirm"),
      message: t("explorer.deleteProjectMessage"),
      confirmText: projectSlug,
      confirmLabel: t("explorer.deleteProject"),
    });
    if (ok) deleteProject.mutate(projectId);
  }

  async function handleDeleteModel(modelId: string, modelSlug: string) {
    const ok = await confirm({
      mode: "typed-name",
      title: t("explorer.deleteModelConfirm"),
      message: t("explorer.deleteModelMessage"),
      confirmText: modelSlug,
      confirmLabel: t("explorer.deleteModel"),
    });
    if (ok) deleteModel.mutate(modelId);
  }

  async function handleDeployModel(m: Model) {
    if (m.deployed_version_id) {
      const ok = await confirm({
        mode: "typed-name",
        title: t("explorer.undeployConfirm"),
        message: t("explorer.undeployMessage"),
          confirmText: t("explorer.undeploy"),
          confirmLabel: t("explorer.undeploy"),
      });
      if (ok) undeployModel.mutate(m.id);
      return;
    }
    try {
      await deployModel.mutateAsync(m.id);
    } catch (err) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? t("explorer.deployFailed");
      await confirm({
        mode: "simple",
        title: t("explorer.cantDeployModel"),
        message: msg,
        confirmLabel: t("common.ok"),
      });
    }
  }

  function openRename(type: "project" | "model", id: string, name: string) {
    setRenameType(type);
    setRenameTargetId(id);
    setRenameValue(name);
    setRenameOpen(true);
  }

  // ── sidebar ──────────────────────────────────────────────────────

  const drawerContent = (
    <Box sx={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <Toolbar />

      {/* Workspace nav item */}
      <List disablePadding>
        <ListItemButton selected sx={{ py: 1.5 }}>
          <ListItemIcon sx={{ minWidth: 40 }}>
            <DashboardIcon color="primary" />
          </ListItemIcon>
          <ListItemText
            primary={t("explorer.workspace")}
            primaryTypographyProps={{ fontWeight: 600 }}
          />
        </ListItemButton>
      </List>

      {/* Tenant card */}
      <Box px={2} pt={2} pb={1}>
        <Card
          elevation={0}
          sx={{
            bgcolor: theme.palette.grey[100],
            borderRadius: 1,
            border: `1px solid ${theme.palette.grey[200]}`,
          }}
        >
          <CardContent
            sx={{
              p: 1.5,
              "&:last-child": { pb: 1.5 },
              display: "flex",
              alignItems: "center",
              gap: 1.5,
            }}
          >
            <BusinessIcon color="primary" fontSize="small" />
            <Box>
              <Typography variant="body2" fontWeight={600}>
                {tenant.data?.display_name ?? tenantSlug}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {t("explorer.tenant")}
              </Typography>
            </Box>
          </CardContent>
        </Card>
      </Box>

      {/* Import / Export — workspace-level action, tenant admin only */}
      {canPerform("project.importExport") && (
        <Box px={2} pb={1}>
          <Button
            fullWidth
            size="small"
            startIcon={<ImportExportIcon sx={{ fontSize: 18 }} />}
            onClick={() => setProjectImportExportOpen(true)}
            sx={{
              justifyContent: "flex-start",
              textTransform: "none",
              fontWeight: 500,
              color: "text.secondary",
              borderRadius: 1,
              px: 1.5,
              "&:hover": { bgcolor: theme.palette.action.hover },
            }}
            aria-label={t("explorer.projectImportExport")}
          >
            {t("explorer.projectImportExport")}
          </Button>
        </Box>
      )}

      <Divider sx={{ mx: 2, mb: 1 }} />

      {/* Projects header */}
      <Box sx={{ display: "flex", alignItems: "center", px: 2, pb: 1 }}>
        <Typography
          variant="subtitle2"
          fontWeight={700}
          color="text.secondary"
          flexGrow={1}
        >
          {t("explorer.projects")}
        </Typography>
        {canPerform("project.create") && (
          <Tooltip title={t("explorer.createProject")}>
            <IconButton
              size="small"
              aria-label={t("explorer.createProject")}
              onClick={() => openDialog("project")}
              sx={{ color: "primary.main" }}
            >
              <AddIcon fontSize="small" />
            </IconButton>
          </Tooltip>
        )}
      </Box>

      {/* Project search */}
      <Box sx={{ px: 2, pb: 1 }}>
        <TextField
          size="small"
          fullWidth
          placeholder={t("explorer.filterProjects")}
          value={projectFilter}
          onChange={(e) => setProjectFilter(e.target.value)}
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <SearchIcon sx={{ fontSize: 18, color: "text.disabled" }} />
              </InputAdornment>
            ),
          }}
          sx={{
            "& .MuiOutlinedInput-root": { height: 32, fontSize: "0.8rem" },
          }}
        />
      </Box>

      {/* Project list */}
      <Box sx={{ flex: 1, overflow: "auto" }}>
        {projects.isLoading && (
          <Box p={2} textAlign="center">
            <CircularProgress size={20} />
          </Box>
        )}
        <List dense disablePadding>
          {sortedProjects.map((p) => {
            const active = selectedProject?.id === p.id;
            const isPinned = pinnedProjects.includes(p.id);
            return (
              <ListItemButton
                key={p.id}
                selected={active}
                onClick={() => setSelectedProject(p)}
                aria-label={t("explorer.selectProject") + " " + p.display_name}
                sx={{ py: 1, pr: 8 }}
              >
                <ListItemIcon sx={{ minWidth: 36 }}>
                  {active ? (
                    <FolderOpenIcon fontSize="small" sx={{ color: "primary.main" }} />
                  ) : (
                    <FolderIcon fontSize="small" color="action" />
                  )}
                </ListItemIcon>
                <ListItemText
                  primary={p.display_name}
                  secondary={p.slug}
                  primaryTypographyProps={{
                    fontWeight: active ? 600 : 400,
                    variant: "body2",
                  }}
                  secondaryTypographyProps={{ variant: "caption" }}
                />
                <Box
                  sx={{
                    position: "absolute",
                    right: 4,
                    top: "50%",
                    transform: "translateY(-50%)",
                    display: "flex",
                    gap: 0.25,
                  }}
                >
                  <Tooltip title={isPinned ? t("common.unpin") : t("common.pin")}>
                    <IconButton
                      size="small"
                      aria-label={`${isPinned ? t("common.unpin") : t("common.pin")} ${p.display_name}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        togglePinProject(p.id);
                      }}
                    >
                      {isPinned ? (
                        <StarIcon sx={{ fontSize: 16, color: "warning.main" }} />
                      ) : (
                        <StarBorderIcon sx={{ fontSize: 16 }} />
                      )}
                    </IconButton>
                  </Tooltip>
                  {canPerform("project.rename") && (
                    <Tooltip title={t("common.rename")}>
                      <IconButton
                        size="small"
                        aria-label={`${t("common.rename")} ${p.display_name}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          openRename("project", p.id, p.display_name);
                        }}
                      >
                        <EditIcon sx={{ fontSize: 16 }} />
                      </IconButton>
                    </Tooltip>
                  )}
                  {canPerform("project.delete") && (
                    <Tooltip title={t("common.delete")}>
                      <IconButton
                        size="small"
                        aria-label={`${t("common.delete")} ${p.display_name}`}
                        onClick={(e) => {
                          e.stopPropagation();
                          handleDeleteProject(p.id, p.slug);
                        }}
                      >
                        <DeleteIcon sx={{ fontSize: 16 }} />
                      </IconButton>
                    </Tooltip>
                  )}
                </Box>
              </ListItemButton>
            );
          })}
        </List>
      </Box>
    </Box>
  );

  // ── main content ─────────────────────────────────────────────────

  return (
    <Box sx={{ display: "flex", height: "100vh", overflow: "hidden" }}>
      {isMobile && (
        <IconButton
          color="inherit"
          aria-label={t("explorer.openDrawer")}
          edge="start"
          onClick={() => setMobileOpen(true)}
          sx={{
            position: "fixed",
            top: 12,
            left: 16,
            zIndex: theme.zIndex.appBar + 2,
            color: "white",
          }}
        >
          <MenuIcon />
        </IconButton>
      )}

      <Box
        component="nav"
        sx={{ width: { md: DRAWER_WIDTH }, flexShrink: { md: 0 } }}
      >
        <Drawer
          variant={isMobile ? "temporary" : "permanent"}
          open={isMobile ? mobileOpen : true}
          onClose={() => setMobileOpen(false)}
          ModalProps={{ keepMounted: true }}
          sx={{
            "& .MuiDrawer-paper": {
              boxSizing: "border-box",
              width: DRAWER_WIDTH,
            },
          }}
        >
          {drawerContent}
        </Drawer>
      </Box>

      <Box
        component="main"
        sx={{
          flexGrow: 1,
          p: 3,
          pt: 11,
          height: "100vh",
          overflow: "auto",
          bgcolor: "background.default",
        }}
      >
        {!selectedProject ? (
          <Box
            display="flex"
            alignItems="center"
            justifyContent="center"
            height="100%"
          >
            <Typography color="text.secondary" fontWeight={500}>
              {t("explorer.selectOrCreateProject")}
            </Typography>
          </Box>
        ) : (
          <Box maxWidth="lg" mx="auto">
            {/* ── Project header ───────────────────────────────── */}
            <Box
              sx={{
                position: "sticky",
                top: -24,
                bgcolor: "background.default",
                zIndex: 10,
                pb: 2,
                pt: 1,
                mb: 3,
                borderBottom: 1,
                borderColor: "divider",
                display: "flex",
                alignItems: "center",
                gap: 2,
              }}
            >
              <Box flexGrow={1}>
                <Typography variant="h5" fontWeight={600} color="text.primary">
                  {selectedProject.display_name}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {selectedProject.slug}
                </Typography>
              </Box>
              <HelpIconButton href="/help/getting-started/workspace-explorer.html" />

              {/* Enabled toggle — enabling/disabling a project is structural;
                  backend PATCH requires admin, UI gated to tenant admin. */}
              {canPerform("project.toggleActive") && (
                <FormControlLabel
                  control={
                    <Switch
                      size="small"
                      checked={selectedProject.is_active}
                      onChange={(e) =>
                        toggleProjectActive.mutate({
                          projectId: selectedProject.id,
                          enabled: e.target.checked,
                        })
                      }
                      disabled={toggleProjectActive.isPending}
                      sx={{
                        "& .MuiSwitch-switchBase.Mui-checked": {
                          color: "primary.main",
                        },
                        "& .MuiSwitch-switchBase.Mui-checked + .MuiSwitch-track": {
                          backgroundColor: "primary.main",
                        },
                      }}
                      inputProps={{ "aria-label": t("common.enabled") }}
                    />
                  }
                  label={
                    <Typography variant="body2" color="text.secondary">
                      {t("common.enabled")}
                    </Typography>
                  }
                  sx={{ mr: 1 }}
                />
              )}

              <Divider
                orientation="vertical"
                flexItem
                sx={{ mx: 0.5, my: 1 }}
              />

              {/* Add Model — icon button matching Add Project style */}
              {canPerform("model.add") && (
                <Tooltip title={t("explorer.addModel")}>
                  <IconButton
                    onClick={() => openDialog("model")}
                    aria-label={t("explorer.addModel")}
                    sx={{ color: "primary.main" }}
                  >
                    <AddIcon />
                  </IconButton>
                </Tooltip>
              )}

              {canPerform("model.importExport") && (
                <Tooltip title={t("explorer.modelImportExport")}>
                  <IconButton
                    onClick={() => setModelImportExportOpen(true)}
                    aria-label={t("explorer.modelImportExport")}
                    sx={{ color: "text.secondary" }}
                  >
                    <ImportExportIcon />
                  </IconButton>
                </Tooltip>
              )}

              {/* Project setup — opens the config drawer. Modellers reach the
                  agent-config group; admin-only sections (connections, LLM,
                  branding, access control, audit, webhooks, SSO) are gated
                  inside the drawer. Modeller+ may open it. */}
              {canPerform("project.configDrawer") && (
                <Tooltip title={t("explorer.projectSettings")}>
                  <IconButton
                    onClick={() => setConfigProject(selectedProject)}
                    aria-label={t("explorer.projectSettings")}
                    sx={{ color: "text.secondary" }}
                  >
                    <SettingsIcon />
                  </IconButton>
                </Tooltip>
              )}
            </Box>

            {/* ── Models ─────────────────────────────────────── */}

                {/* Conversational Agent — feature card */}
                <Paper
                  elevation={0}
                  sx={{
                    mb: 3,
                    p: 3,
                    border: `1px solid ${theme.palette.grey[300]}`,
                    borderRadius: 1,
                    display: "flex",
                    alignItems: "center",
                    gap: 3,
                  }}
                >
                  <AutoAwesomeIcon
                    sx={{
                      fontSize: 32,
                      flexShrink: 0,
                      color: agentConfigQuery.data?.enabled ? "primary.main" : theme.palette.text.disabled,
                    }}
                  />
                  <Box sx={{ flex: 1 }}>
                    <Box display="flex" alignItems="center" gap={1} mb={0.5}>
                      <Typography variant="subtitle1" fontWeight={700}>
                        {t("explorer.conversationalAgent")}
                      </Typography>
                      <Chip
                        size="small"
                        label={agentConfigQuery.data?.enabled ? t("common.enabled") : t("common.disabled")}
                        color={agentConfigQuery.data?.enabled ? "success" : "default"}
                        variant={agentConfigQuery.data?.enabled ? "filled" : "outlined"}
                        sx={{ height: 22, fontSize: "0.7rem" }}
                      />
                    </Box>
                    <Typography variant="body2" color="text.secondary" mb={1.5}>
                      {t("explorer.agentDescription")}
                      {agentConfigQuery.data?.display_name
                        ? ` ${t("explorer.poweredBy", { name: agentConfigQuery.data.display_name })}`
                        : ""}
                    </Typography>
                    <Box display="flex" gap={1.5}>
                      <Button
                        variant="contained"
                        size="small"
                        disableElevation
                        startIcon={<ChatIcon />}
                        onClick={() =>
                          navigate(`/tenants/${tenantSlug}/projects/${selectedProject.id}/agent`)
                        }
                        sx={{
                          bgcolor: "primary.main",
                          "&:hover": { bgcolor: "primary.dark" },
                          textTransform: "none",
                          fontWeight: 600,
                        }}
                      >
                        {t("explorer.startConversation")}
                      </Button>
                      {agentConfigQuery.data?.enable_agent_log_screen && (
                        <Button
                          variant="outlined"
                          size="small"
                          onClick={() =>
                            navigate(`/tenants/${tenantSlug}/projects/${selectedProject.id}/agent-log`)
                          }
                          sx={{
                            textTransform: "none",
                            borderColor: theme.palette.grey[400],
                            color: "text.secondary",
                          }}
                        >
                          {t("explorer.chatHistory")}
                        </Button>
                      )}
                    </Box>
                  </Box>
                </Paper>

                {/* Model grid */}
            {models.isLoading && <CircularProgress size={24} />}

            {models.data?.length === 0 && (
              <Paper
                elevation={0}
                sx={{
                  p: 6,
                  textAlign: "center",
                  bgcolor: theme.palette.grey[100],
                  border: `1px dashed ${theme.palette.grey[400]}`,
                  borderRadius: 2,
                }}
              >
                <StorageIcon
                  sx={{
                    fontSize: 48,
                    color: theme.palette.grey[400],
                    mb: 2,
                  }}
                />
                <Typography color="text.secondary" fontWeight={500}>
                  {t("explorer.noModels")}
                </Typography>
              </Paper>
            )}

            <Box
              sx={{
                display: "grid",
                gridTemplateColumns: {
                  xs: "1fr",
                  sm: "repeat(2, 1fr)",
                  md: "repeat(3, 1fr)",
                },
                gap: 3,
              }}
            >
              {sortedModels.map((m) => {
                const isDeployed = Boolean(m.deployed_version_id);
                const isModelPinned = pinnedModels.includes(m.id);
                return (
                  <Card
                    key={m.id}
                    elevation={0}
                    sx={{
                      border: `1px solid ${theme.palette.grey[300]}`,
                      borderRadius: 1,
                      display: "flex",
                      flexDirection: "column",
                      height: "100%",
                    }}
                  >
                    <CardActionArea
                      onClick={() => handleModelClick(m)}
                      sx={{
                        flexGrow: 1,
                        p: 2,
                        display: "flex",
                        flexDirection: "column",
                        alignItems: "flex-start",
                        justifyContent: "flex-start",
                      }}
                      aria-label={t("explorer.openModel") + " " + m.display_name}
                    >
                      <Box
                        display="flex"
                        alignItems="flex-start"
                        width="100%"
                      >
                        <Box
                          display="flex"
                          alignItems="center"
                          gap={1.5}
                          flexGrow={1}
                        >
                          <StorageIcon
                            fontSize="medium"
                            sx={{ color: "primary.main" }}
                          />
                          <Box flexGrow={1}>
                            <Box display="flex" alignItems="center" gap={1}>
                              <Typography fontWeight={600} variant="body1">
                                {m.display_name}
                              </Typography>
                              <Chip
                                size="small"
                                label={isDeployed ? t("common.deployed") : t("common.draft")}
                                color={isDeployed ? "success" : "default"}
                                variant={isDeployed ? "filled" : "outlined"}
                                sx={{ height: 20, fontSize: "0.65rem" }}
                              />
                            </Box>
                            <Typography
                              variant="caption"
                              color="text.secondary"
                            >
                              {m.slug}
                              {m.last_deployed_at && (
                                <>
                                  {" "}
                                  {t("explorer.deployedStatus", {
                                    when: new Date(
                                      m.last_deployed_at,
                                    ).toLocaleDateString(),
                                  })}
                                </>
                              )}
                            </Typography>
                          </Box>
                        </Box>
                      </Box>
                    </CardActionArea>

                    <Divider />

                    {/* Model footer — deploy toggle + hover-reveal rename/delete */}
                    <Box
                      px={1.5}
                      py={0.75}
                      display="flex"
                      alignItems="center"
                      bgcolor={theme.palette.grey[50]}
                    >
                      {/* Deploy / Undeploy — backend POST requires modeler */}
                      {canPerform("model.deploy") && (
                        <Tooltip title={isDeployed ? t("explorer.undeploy") : t("explorer.deploy")}>
                          <IconButton
                            size="small"
                            disabled={
                              deployModel.isPending || undeployModel.isPending
                            }
                            onClick={(e) => {
                              e.stopPropagation();
                              void handleDeployModel(m);
                            }}
                            aria-label={`${isDeployed ? t("explorer.undeploy") : t("explorer.deploy")} ${m.display_name}`}
                            sx={{
                              color: isDeployed
                                ? "primary.main"
                                : theme.palette.text.disabled,
                            }}
                          >
                            <RocketLaunchIcon sx={{ fontSize: 20 }} />
                          </IconButton>
                        </Tooltip>
                      )}

                      <Box flexGrow={1} />

                      <Box
                        sx={{
                          display: "flex",
                          gap: 0.25,
                        }}
                      >
                        <Tooltip title={isModelPinned ? t("common.unpin") : t("common.pin")}>
                          <IconButton
                            size="small"
                            onClick={(e) => {
                              e.stopPropagation();
                              togglePinModel(m.id);
                            }}
                            aria-label={`${isModelPinned ? t("common.unpin") : t("common.pin")} ${m.display_name}`}
                          >
                            {isModelPinned ? (
                              <StarIcon sx={{ fontSize: 16, color: "warning.main" }} />
                            ) : (
                              <StarBorderIcon sx={{ fontSize: 16 }} />
                            )}
                          </IconButton>
                        </Tooltip>
                        {canPerform("model.rename") && (
                          <Tooltip title={t("common.rename")}>
                            <IconButton
                              size="small"
                              onClick={(e) => {
                                e.stopPropagation();
                                openRename("model", m.id, m.display_name);
                              }}
                              aria-label={`${t("common.rename")} ${m.display_name}`}
                            >
                              <EditIcon sx={{ fontSize: 16 }} />
                            </IconButton>
                          </Tooltip>
                        )}
                        {canPerform("model.delete") && (
                          <Tooltip title={t("common.delete")}>
                            <IconButton
                              size="small"
                              onClick={(e) => {
                                e.stopPropagation();
                                handleDeleteModel(m.id, m.slug);
                              }}
                              aria-label={`${t("common.delete")} ${m.display_name}`}
                            >
                              <DeleteIcon sx={{ fontSize: 16 }} />
                            </IconButton>
                          </Tooltip>
                        )}
                      </Box>
                    </Box>
                  </Card>
                );
              })}
            </Box>

          </Box>
        )}
      </Box>

      {/* ── Dialogs ──────────────────────────────────────────── */}

      {/* Rename */}
      <Dialog
        open={renameOpen}
        onClose={() => setRenameOpen(false)}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle>
          {renameType === "project" ? t("explorer.renameProject") : t("explorer.renameModel")}
        </DialogTitle>
        <DialogContent>
          <TextField
            label={t("common.displayName")}
            fullWidth
            margin="normal"
            value={renameValue}
            onChange={(e) => setRenameValue(e.target.value)}
            autoFocus
            inputProps={{ "aria-label": t("common.displayName") }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRenameOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => {
              if (renameType === "project") {
                updateProject.mutate({
                  projectId: renameTargetId,
                  display_name: renameValue,
                });
              } else {
                updateModel.mutate({
                  modelId: renameTargetId,
                  display_name: renameValue,
                });
              }
            }}
            disabled={
              !renameValue ||
              updateProject.isPending ||
              updateModel.isPending
            }
            sx={{ bgcolor: "primary.main", "&:hover": { bgcolor: "primary.dark" } }}
          >
            {updateProject.isPending || updateModel.isPending ? (
              <CircularProgress size={18} color="inherit" />
            ) : (
              t("common.save")
            )}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Create Project */}
      <Dialog
        open={newProjectOpen}
        onClose={() => setNewProjectOpen(false)}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle>{t("explorer.newProject")}</DialogTitle>
        <DialogContent>
          <TextField
            label={t("common.slugHelp")}
            fullWidth
            margin="normal"
            value={formSlug}
            onChange={(e) => setFormSlug(e.target.value)}
            autoFocus
            inputProps={{ "aria-label": t("common.slug") }}
          />
          <TextField
            label={t("common.displayName")}
            fullWidth
            margin="normal"
            value={formName}
            onChange={(e) => setFormName(e.target.value)}
            helperText={t("common.optionalDefaultsToSlug")}
            inputProps={{ "aria-label": t("common.displayName") }}
          />
          {createProject.isError && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {t("explorer.failedToCreateProject")}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setNewProjectOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => createProject.mutate()}
            disabled={!formSlug || createProject.isPending}
            sx={{ bgcolor: "primary.main", "&:hover": { bgcolor: "primary.dark" } }}
          >
            {createProject.isPending ? (
              <CircularProgress size={18} color="inherit" />
            ) : (
              t("common.create")
            )}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Create Model */}
      <Dialog
        open={newModelOpen}
        onClose={() => setNewModelOpen(false)}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle>{t("explorer.newModel")}</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" mb={1}>
            {t("explorer.creatingModelInProject")} {selectedProject?.display_name}
          </Typography>
          <TextField
            label={t("common.slugHelp")}
            fullWidth
            margin="normal"
            value={formSlug}
            onChange={(e) => setFormSlug(e.target.value)}
            autoFocus
            inputProps={{ "aria-label": t("common.slug") }}
          />
          <TextField
            label={t("common.displayName")}
            fullWidth
            margin="normal"
            value={formName}
            onChange={(e) => setFormName(e.target.value)}
            helperText={t("common.optionalDefaultsToSlug")}
            inputProps={{ "aria-label": t("common.displayName") }}
          />
          {createModel.isError && (
            <Alert severity="error" sx={{ mt: 1 }}>
              {t("explorer.failedToCreateModel")}
            </Alert>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setNewModelOpen(false)}>{t("common.cancel")}</Button>
          <Button
            variant="contained"
            onClick={() => createModel.mutate()}
            disabled={!formSlug || createModel.isPending}
            sx={{ bgcolor: "primary.main", "&:hover": { bgcolor: "primary.dark" } }}
          >
            {createModel.isPending ? (
              <CircularProgress size={18} color="inherit" />
            ) : (
              t("common.create")
            )}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Project Config Drawer */}
      <ProjectConfigDrawer
        project={configProject}
        open={configProject !== null}
        onClose={() => {
          setConfigProject(null);
          if (selectedProject?.id) {
            qc.invalidateQueries({ queryKey: ["agent-config", selectedProject.id] });
          }
        }}
      />

      {/* Project Import/Export — tenant level */}
      <ProjectImportExportDialog
        open={projectImportExportOpen}
        onClose={() => setProjectImportExportOpen(false)}
        onProjectImported={() => qc.invalidateQueries({ queryKey: ["projects"] })}
      />

      {/* Model Import/Export — project level */}
      {selectedProject && (
        <ModelImportExportDialog
          open={modelImportExportOpen}
          onClose={() => setModelImportExportOpen(false)}
          projectId={selectedProject.id}
          projectSlug={selectedProject.slug}
          onModelImported={(modelId) =>
            navigate(
              `/tenants/${tenantSlug}/projects/${selectedProject.id}/models/${modelId}`,
            )
          }
        />
      )}
    </Box>
  );
}
