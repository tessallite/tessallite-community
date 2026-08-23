import { lazy, Suspense, useEffect, useState } from "react";
import { safeLocalGet } from "./utils/safeLocalStorage";
import {
  createBrowserRouter,
  Navigate,
  Outlet,
  RouterProvider,
} from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Box, CircularProgress } from "@mui/material";
import Layout from "./components/Layout";
import RequireAuth from "./components/RequireAuth";
import { ConfirmProvider } from "./components/Confirm";
import ChunkErrorBoundary from "./components/ChunkErrorBoundary";
import SessionExpiredOverlay from "./components/SessionExpiredOverlay";
import { authApi } from "./api/client";
import { useBuilderStore } from "./store/builderStore";
import { I18nContext, getMessages, loadLocale, RTL_LOCALES } from "./i18n";
import Login from "./pages/Login";

// Route-level code splitting. Login stays eager because it's the landing
// page every new session hits; everything else loads on demand.
const Explorer = lazy(() => import("./pages/Explorer"));
const ModelBuilder = lazy(() => import("./pages/ModelBuilder"));
const PublicGlossary = lazy(() => import("./pages/PublicGlossary"));
const SystemAdmin = lazy(() => import("./pages/SystemAdmin"));
const SystemConfiguration = lazy(
  () => import("./pages/SystemConfiguration"),
);
const LicenseEdition = lazy(() => import("./pages/LicenseEdition"));
const AgentChat = lazy(() => import("./pages/AgentChat"));
const AgentLog = lazy(() => import("./pages/AgentLog"));
const AuditLog = lazy(() => import("./pages/AuditLog"));
const WelcomeWizard = lazy(
  () => import("./components/WelcomeWizard/WelcomeWizard"),
);
const GroupMappings = lazy(() => import("./pages/GroupMappings"));
const SsoCallback = lazy(() => import("./pages/SsoCallback"));
const WebhooksPage = lazy(() => import("./pages/Webhooks"));
const AccessTokens = lazy(() => import("./pages/AccessTokens"));

function RouteFallback() {
  return (
    <Box
      sx={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        minHeight: "40vh",
      }}
    >
      <CircularProgress size={28} />
    </Box>
  );
}

function IndexRedirect() {
  const role = safeLocalGet("user_role", "");
  if (role === "system_admin") return <Navigate to="/system" replace />;

  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => authApi.me(),
    staleTime: 60_000,
    retry: false,
  });

  if (me.isLoading) return <RouteFallback />;

  if (me.data?.role && me.data.role !== safeLocalGet("user_role", "")) {
    localStorage.setItem("user_role", me.data.role);
  }

  // The welcome wizard walks through setup actions — add sources, build a
  // model, run a query, connect BI — which only an admin or modeler can do.
  // It was previously shown to every role EXCEPT tenant_admin, i.e. exactly
  // backwards: viewers (who can do none of the steps) saw it and the tenant
  // admin (the person the walkthrough is for) never did. Show it only to the
  // setup-capable roles (F-029-19).
  const SETUP_ROLES = ["tenant_admin", "modeler"];
  if (
    me.data &&
    !me.data.has_completed_onboarding &&
    SETUP_ROLES.includes(me.data.role)
  ) {
    return <Navigate to="/welcome" replace />;
  }

  return <Explorer />;
}

function RootShell() {
  const displayLocale = useBuilderStore((s) => s.displayLocale);
  // Bug-7726: locale bundles are lazy-loaded. Trigger the async load when
  // the display locale changes, then re-render once the bundle is cached.
  const [, setLocaleVersion] = useState(0);
  useEffect(() => {
    if (displayLocale) {
      loadLocale(displayLocale).then(() => setLocaleVersion((v) => v + 1));
    }
  }, [displayLocale]);
  const messages = getMessages(displayLocale);

  // Bug-6509: keep <html lang> and dir in sync with the active UI locale.
  useEffect(() => {
    const lang = displayLocale ? displayLocale.split("-")[0] : "en";
    document.documentElement.lang = lang;
    document.documentElement.dir = RTL_LOCALES.has(lang) ? "rtl" : "ltr";
  }, [displayLocale]);

  return (
    <I18nContext.Provider value={messages}>
      <ConfirmProvider>
        <SessionExpiredOverlay />
        <Suspense fallback={<RouteFallback />}>
          <Outlet />
        </Suspense>
      </ConfirmProvider>
    </I18nContext.Provider>
  );
}

// Data router — required so `useBlocker` (the unsaved-edits navigation
// guard in the Model Builder) has a router context to attach to.
const router = createBrowserRouter([
  {
    element: <RootShell />,
    children: [
      { path: "/login", element: <Login /> },
      { path: "/sso/callback", element: <SsoCallback /> },
      { path: "/g/:token", element: <PublicGlossary /> },
      {
        path: "/",
        element: (
          <RequireAuth>
            <Layout />
          </RequireAuth>
        ),
        children: [
          { index: true, element: <IndexRedirect /> },
          { path: "welcome", element: <WelcomeWizard /> },
          { path: "account/tokens", element: <AccessTokens /> },
          { path: "system", element: <SystemAdmin /> },
          { path: "system/configuration", element: <SystemConfiguration /> },
          { path: "system/license", element: <LicenseEdition /> },
          { path: "admin", element: <Navigate to="/" replace /> },
          { path: "admin/audit-log", element: <AuditLog /> },
          { path: "admin/group-mappings", element: <GroupMappings /> },
          { path: "admin/webhooks", element: <WebhooksPage /> },
          {
            path: "tenants/:tenantId/projects/:projectId/models/:modelId",
            element: <ModelBuilder />,
          },
          {
            path: "tenants/:tenantId/projects/:projectId/agent",
            element: <AgentChat />,
          },
          {
            path: "tenants/:tenantId/projects/:projectId/agent-log",
            element: <AgentLog />,
          },
        ],
      },
      { path: "*", element: <Navigate to="/" replace /> },
    ],
  },
]);

export default function App() {
  return (
    <ChunkErrorBoundary>
      <RouterProvider router={router} />
    </ChunkErrorBoundary>
  );
}
