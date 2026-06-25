import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { safeLocalGet } from "../utils/safeLocalStorage";
import { brandingApi } from "../api/client";
import { Outlet, useNavigate, useParams, useLocation } from "react-router-dom";
import { useT } from "../i18n";
import { useLanguagePreference } from "../hooks/useLanguagePreference";
import {
  AppBar,
  Box,
  Drawer,
  IconButton,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Menu,
  MenuItem,
  Toolbar,
  Tooltip,
  Typography,
} from "@mui/material";
import AdminPanelSettingsIcon from "@mui/icons-material/AdminPanelSettings";
import AccountCircleIcon from "@mui/icons-material/AccountCircle";
import SettingsIcon from "@mui/icons-material/Settings";
import WorkspacePremiumIcon from "@mui/icons-material/WorkspacePremium";
import LocaleSelector from "./LocaleSelector";
import EditionBadge from "./EditionBadge";

const DRAWER_WIDTH = 220;

// Top AppBar is 64px on desktop; keep content a generous gap below it.
// UI guideline: never let page content touch the top stripe.
const APP_BAR_OFFSET = 9; // theme.spacing(9) = 72px (64 AppBar + 8 breathing room)

export default function Layout() {
  const navigate = useNavigate();
  const location = useLocation();
  const t = useT();
  useLanguagePreference();
  const params = useParams<{
    tenantId?: string;
    projectId?: string;
    modelId?: string;
  }>();

  const userRole = safeLocalGet("user_role", "");
  const isSystemAdmin = userRole === "system_admin";
  const canOpenConfig = isSystemAdmin;

  const tenantId = safeLocalGet("tenant_id", "");
  const brandingQuery = useQuery({
    queryKey: ["branding", tenantId],
    queryFn: () => brandingApi.get(tenantId),
    enabled: Boolean(tenantId),
    staleTime: 5 * 60_000,
  });
  const branding = brandingQuery.data;

  const isBuilderRoute = !!params.modelId;
  const isExplorerRoute = location.pathname === "/";
  const hideLayoutDrawer = isBuilderRoute || isExplorerRoute || !isSystemAdmin;

  const [anchorEl, setAnchorEl] = useState<null | HTMLElement>(null);

  function handleMenuOpen(event: React.MouseEvent<HTMLElement>) {
    setAnchorEl(event.currentTarget);
  }

  function handleMenuClose() {
    setAnchorEl(null);
  }

  function handleLogout() {
    handleMenuClose();
    import("../api/client").then(({ authApi }) => {
      authApi.logout().catch((e: unknown) => console.warn("Logout request failed:", e));
    });
    localStorage.removeItem("tenant_id");
    localStorage.removeItem("user_role");
    navigate("/login");
  }

  function handleOpenConfig() {
    if (isSystemAdmin) navigate("/system/configuration");
  }

  const onConfigRoute = location.pathname === "/system/configuration";
  const onLicenseRoute = location.pathname === "/system/license";

  return (
    <Box sx={{ display: "flex" }}>
      <AppBar
        position="fixed"
        elevation={2}
        sx={{ zIndex: (t) => t.zIndex.drawer + 1, bgcolor: "primary.main" }}
      >
        <Toolbar>
          <Box display="flex" alignItems="center" gap={1.5} sx={{ flexGrow: 1, cursor: "pointer" }} onClick={() => navigate("/")}>
            <img src={branding?.logo_url || "/favicon.png"} alt={t("nav.logoAlt")} width={28} height={28} />
            <Typography
              variant="h6"
              noWrap
              sx={{ fontWeight: 700 }}
            >
              {branding?.app_title || t("branding.appTitleFallback")}
            </Typography>
          </Box>
          {isSystemAdmin && (
            <Typography variant="body2" sx={{ opacity: 0.7, mr: 2 }}>
              {t("nav.systemAdmin")}
            </Typography>
          )}

          {isSystemAdmin && (
            <Tooltip title={t("nav.licenseEdition")}>
              <IconButton
                size="large"
                aria-label={t("nav.aria.openLicense")}
                onClick={() => navigate("/system/license")}
                color="inherit"
                sx={{ opacity: onLicenseRoute ? 1 : 0.85, mr: 0.5 }}
              >
                <WorkspacePremiumIcon />
              </IconButton>
            </Tooltip>
          )}

          {canOpenConfig && (
            <Tooltip title={t("nav.configuration")}>
              <IconButton
                size="large"
                aria-label={t("nav.aria.openConfiguration")}
                onClick={handleOpenConfig}
                color="inherit"
                sx={{ opacity: onConfigRoute ? 1 : 0.85, mr: 0.5 }}
              >
                <SettingsIcon />
              </IconButton>
            </Tooltip>
          )}

          <EditionBadge />

          <LocaleSelector />

          <IconButton
            size="large"
            edge="end"
            aria-label={t("nav.aria.accountMenu")}
            aria-controls="menu-appbar"
            aria-haspopup="true"
            onClick={handleMenuOpen}
            color="inherit"
          >
            <AccountCircleIcon />
          </IconButton>
          <Menu
            id="menu-appbar"
            anchorEl={anchorEl}
            anchorOrigin={{
              vertical: "bottom",
              horizontal: "right",
            }}
            keepMounted
            transformOrigin={{
              vertical: "top",
              horizontal: "right",
            }}
            open={Boolean(anchorEl)}
            onClose={handleMenuClose}
          >
            <MenuItem onClick={handleMenuClose}>{t("nav.profile")}</MenuItem>
            <MenuItem onClick={() => { handleMenuClose(); navigate("/welcome"); }}>
              {t("nav.welcomeTour")}
            </MenuItem>
            <MenuItem onClick={handleLogout}>{t("nav.logout")}</MenuItem>
          </Menu>
        </Toolbar>
      </AppBar>

      {!hideLayoutDrawer && (
        <Drawer
          variant="permanent"
          sx={{
            width: DRAWER_WIDTH,
            flexShrink: 0,
            "& .MuiDrawer-paper": {
              width: DRAWER_WIDTH,
              boxSizing: "border-box",
            },
          }}
        >
          <Toolbar />
          <Box sx={{ overflow: "auto" }}>
            <List dense>
              {isSystemAdmin && (
                <ListItemButton
                  onClick={() => navigate("/")}
                  sx={{ borderRadius: 1, mx: 0.5 }}
                >
                  <ListItemIcon sx={{ minWidth: 36 }}>
                    <AdminPanelSettingsIcon />
                  </ListItemIcon>
                  <ListItemText primary={t("nav.tenants")} />
                </ListItemButton>
              )}
              {isSystemAdmin && (
                <ListItemButton
                  selected={onLicenseRoute}
                  onClick={() => navigate("/system/license")}
                  sx={{ borderRadius: 1, mx: 0.5 }}
                >
                  <ListItemIcon sx={{ minWidth: 36 }}>
                    <WorkspacePremiumIcon />
                  </ListItemIcon>
                  <ListItemText primary={t("nav.licenseEdition")} />
                </ListItemButton>
              )}
            </List>
          </Box>
        </Drawer>
      )}

      <Box
        component="main"
        sx={{
          flexGrow: 1,
          p: 0,
          mt: isExplorerRoute ? 0 : APP_BAR_OFFSET,
          // When the AppBar is shown (non-explorer routes), reserve the
          // offset + breathing room so page content never tucks under the
          // top stripe. Inner pages control their own padding.
          height: isExplorerRoute
            ? "100vh"
            : `calc(100vh - ${APP_BAR_OFFSET * 8}px)`,
          display: "flex",
          flexDirection: "column",
          minWidth: 0,
          overflow: "hidden",
        }}
      >
        <Outlet />
      </Box>
    </Box>
  );
}
