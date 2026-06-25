import { useMemo, useState } from "react";
import { useT } from "../../i18n";
import {
  Box,
  Drawer,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import CableIcon from "@mui/icons-material/Cable";
import PsychologyIcon from "@mui/icons-material/Psychology";
import TuneIcon from "@mui/icons-material/Tune";
import BadgeIcon from "@mui/icons-material/Badge";
import MenuBookIcon from "@mui/icons-material/MenuBook";
import ShieldIcon from "@mui/icons-material/Shield";
import SettingsIcon from "@mui/icons-material/Settings";
import PeopleIcon from "@mui/icons-material/People";
import HistoryIcon from "@mui/icons-material/History";
import WebhookIcon from "@mui/icons-material/Webhook";
import GroupWorkIcon from "@mui/icons-material/GroupWork";
import VerifiedUserIcon from "@mui/icons-material/VerifiedUser";
import PaletteIcon from "@mui/icons-material/Palette";
import ScienceIcon from "@mui/icons-material/Science";
import type { Project } from "../../api/types";
import ConnectionsPanel from "../Panels/ConnectionsPanel";
import ProjectLLMScreen from "./ProjectLLMScreen";
import BrandingPanel from "./BrandingPanel";
import DemoTenantPanel from "./DemoTenantPanel";
import ProjectAgentTabs, { type AgentTabKey } from "./ProjectAgentTabs";
import UsersAccessPanel from "../Admin/UsersAccessPanel";
import SecurityAuditPanel from "../Admin/SecurityAuditPanel";
import AuditLog from "../../pages/AuditLog";
import Webhooks from "../../pages/Webhooks";
import GroupMappings from "../../pages/GroupMappings";
import { isTenantAdmin } from "../../auth/currentUser";
import HelpIconButton from "../HelpIconButton";

type AdminKey =
  | "users-access"
  | "audit-log"
  | "webhooks"
  | "sso-mappings"
  | "security-audit"
  | "demo-tenant";
type SectionKey = "connections" | "llm-providers" | "branding" | AgentTabKey | AdminKey;

const ADMIN_KEYS = new Set<string>([
  "users-access",
  "audit-log",
  "webhooks",
  "sso-mappings",
  "security-audit",
  "demo-tenant",
]);

// Sections that require tenant/project admin. The agent-configuration group is
// modeller-level (backend require_role("modeler") / _require_project_modeller),
// so a modeller may open the drawer and edit those tabs. Everything else writes
// credentials, access bindings, or tenant-level config (backend admin /
// require_tenant_admin) and is hidden from non-admins. See
// docs/architecture/architecture_explorer-rbac-matrix.md.
const ADMIN_SECTIONS = new Set<string>([
  ...ADMIN_KEYS,
  "connections",
  "llm-providers",
  "branding",
]);

type NavItem = { key: SectionKey; label: string; icon: React.ReactNode };
type NavGroup = { group: string; items: NavItem[] };

export default function ProjectConfigDrawer({
  project,
  open,
  onClose,
}: {
  project: Project | null;
  open: boolean;
  onClose: () => void;
}) {
  const t = useT();
  // Admin status is stable for the session (sourced from /users/me). A modeller
  // opens the drawer straight onto their agent-config surface; an admin keeps
  // the connections-first default.
  const admin = isTenantAdmin();
  const [section, setSection] = useState<SectionKey>(admin ? "connections" : "setup");
  const projectId = project?.id ?? "";

  // Admin-only group: source connections, LLM providers and branding all write
  // credentials or tenant-level config (backend admin / require_tenant_admin).
  const PROJECT_NAV: NavGroup = useMemo(() => ({
    group: t("projectNav.project"),
    items: [
      { key: "connections", label: t("projectNav.connections"), icon: <CableIcon fontSize="small" /> },
      { key: "llm-providers", label: t("projectNav.llm"), icon: <PsychologyIcon fontSize="small" /> },
      { key: "branding", label: t("projectNav.branding"), icon: <PaletteIcon fontSize="small" /> },
    ],
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [t]);

  // Modeller-level group: agent configuration is part of model setup.
  const AGENT_NAV: NavGroup = useMemo(() => ({
    group: t("projectNav.agentGroup"),
    items: [
      { key: "setup", label: t("projectNav.setup"), icon: <TuneIcon fontSize="small" /> },
      { key: "identity", label: t("projectNav.identityTone"), icon: <BadgeIcon fontSize="small" /> },
      { key: "knowledge", label: t("projectNav.knowledge"), icon: <MenuBookIcon fontSize="small" /> },
      { key: "guardrails", label: t("projectNav.guardrails"), icon: <ShieldIcon fontSize="small" /> },
      { key: "advanced", label: t("projectNav.advanced"), icon: <SettingsIcon fontSize="small" /> },
    ],
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [t]);

  const ADMIN_NAV: NavGroup = useMemo(() => ({
    group: t("projectNav.adminGroup"),
    items: [
      { key: "users-access", label: t("projectNav.usersAccess"), icon: <PeopleIcon fontSize="small" /> },
      { key: "audit-log", label: t("projectNav.auditLog"), icon: <HistoryIcon fontSize="small" /> },
      { key: "webhooks", label: t("projectNav.webhooks"), icon: <WebhookIcon fontSize="small" /> },
      { key: "sso-mappings", label: t("projectNav.ssoMappings"), icon: <GroupWorkIcon fontSize="small" /> },
      { key: "security-audit", label: t("projectNav.securityAudit"), icon: <VerifiedUserIcon fontSize="small" /> },
      { key: "demo-tenant", label: t("projectNav.demoTenant"), icon: <ScienceIcon fontSize="small" /> },
    ],
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [t]);

  // Modeller sees only the agent-config group; admin sees every group.
  const nav = useMemo<NavGroup[]>(
    () => (admin ? [PROJECT_NAV, AGENT_NAV, ADMIN_NAV] : [AGENT_NAV]),
    [admin, PROJECT_NAV, AGENT_NAV, ADMIN_NAV],
  );

  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={onClose}
      sx={{ zIndex: (t) => t.zIndex.drawer + 2 }}
    >
      <Box sx={{ width: 820, display: "flex", flexDirection: "column", height: "100%" }}>
        <Box
          sx={{
            px: 2,
            py: 1.5,
            borderBottom: 1,
            borderColor: "divider",
            display: "flex",
            alignItems: "center",
          }}
        >
          <Box sx={{ flex: 1 }}>
            <Typography variant="overline" color="text.secondary">
              {t("projectNav.projectConfiguration")}
            </Typography>
            <Typography variant="h6" sx={{ fontWeight: 700 }}>
              {project?.display_name ?? "—"}
            </Typography>
          </Box>
          <HelpIconButton href="/help/admin/project-settings.html" sx={{ mr: 0.5 }} />
          <IconButton onClick={onClose} size="small">
            <CloseIcon />
          </IconButton>
        </Box>

        <Box sx={{ flex: 1, display: "flex", overflow: "hidden" }}>
          <Box
            sx={{
              width: 192,
              flexShrink: 0,
              borderRight: 1,
              borderColor: "divider",
              overflow: "auto",
              bgcolor: "grey.50",
              py: 1,
            }}
          >
            {nav.map((group, gi) => (
              <Box key={group.group} sx={{ mt: gi > 0 ? 1.5 : 0 }}>
                <Typography
                  variant="caption"
                  sx={{
                    px: 2,
                    py: 0.5,
                    display: "block",
                    fontWeight: 700,
                    textTransform: "uppercase",
                    letterSpacing: "0.08em",
                    fontSize: 11,
                    color: "text.secondary",
                  }}
                >
                  {group.group}
                </Typography>
                <List dense disablePadding>
                  {group.items.map((item) => (
                    <ListItemButton
                      key={item.key}
                      selected={section === item.key}
                      onClick={() => setSection(item.key)}
                      sx={{
                        mx: 0.5,
                        borderRadius: 1,
                        py: 0.5,
                        mb: 0.25,
                        "&.Mui-selected": {
                          bgcolor: "primary.main",
                          color: "primary.contrastText",
                          "&:hover": { bgcolor: "primary.dark" },
                          "& .MuiListItemIcon-root": { color: "primary.contrastText" },
                        },
                      }}
                    >
                      <ListItemIcon sx={{ minWidth: 28 }}>{item.icon}</ListItemIcon>
                      <ListItemText
                        primary={item.label}
                        primaryTypographyProps={{
                          variant: "body2",
                          fontWeight: section === item.key ? 600 : 400,
                        }}
                      />
                    </ListItemButton>
                  ))}
                </List>
              </Box>
            ))}
          </Box>

          <Box sx={{ flex: 1, overflow: "auto", p: 2.5 }}>
            {!projectId ? null : ADMIN_SECTIONS.has(section) && !admin ? (
              // Defense in depth: every admin-only section (connections, LLM,
              // branding, user access control, audit, webhooks, SSO, security)
              // is never rendered for a non-admin, even if `section` is forced
              // to an admin key. A modeller falls back to their agent surface.
              <ProjectAgentTabs projectId={projectId} tab="setup" />
            ) : section === "connections" ? (
              <ConnectionsPanel projectId={projectId} />
            ) : section === "llm-providers" ? (
              <ProjectLLMScreen projectId={projectId} />
            ) : section === "branding" ? (
              <BrandingPanel />
            ) : section === "users-access" ? (
              <UsersAccessPanel
                projectId={projectId}
                projectName={project?.display_name ?? ""}
              />
            ) : section === "audit-log" ? (
              <AuditLog />
            ) : section === "webhooks" ? (
              <Webhooks embedded />
            ) : section === "sso-mappings" ? (
              <GroupMappings embedded />
            ) : section === "security-audit" ? (
              <SecurityAuditPanel />
            ) : section === "demo-tenant" ? (
              <DemoTenantPanel />
            ) : (
              <ProjectAgentTabs
                projectId={projectId}
                tab={section as AgentTabKey}
              />
            )}
          </Box>
        </Box>
      </Box>
    </Drawer>
  );
}
