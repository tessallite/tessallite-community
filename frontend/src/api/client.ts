import axios, { type AxiosInstance } from "axios";
import type {
  AggregateCreate,
  AggregateDefinition,
  AggregateUpdate,
  CalendarAutoCreateRequest,
  CalendarBindRequest,
  CalendarCoverageResponse,
  CalendarScriptRequest,
  CalendarScriptResponse,
  CalendarTable,
  CalendarTypeAvailability,
  CalendarUpdateRequest,
  AIOptimizerRun,
  AIOptimizerRunStartResponse,
  AIOptimizerTriggerRequest,
  AISchedulerConfig,
  ModelAISchedulerConfigUpdate,
  LLMConnectionTestRequest,
  LLMConnectionTestResponse,
  LLMProviderConfig,
  LLMProviderConfigCreate,
  LLMProviderConfigUpdate,
  OptimizerRunEntry,
  Connection,
  ConnectionCreate,
  Dimension,
  DiscoveredColumn,
  DimensionCreate,
  DimensionAttributeRelationship,
  DimensionAttributeRelationshipCreate,
  DimensionAttributeRelationshipUpdate,
  Hierarchy,
  HierarchyCreate,
  HierarchyDetail,
  HierarchyUpdate,
  HierarchyGenerateDateRequest,
  HierarchyGeneratedResponse,
  HierarchyGenerateSegmentRequest,
  HierarchyBatchDateRequest,
  HierarchyBatchDateResponse,
  GrainSuggestion,
  HierarchyHealthStatus,
  UnassignedDateColumn,
  DataQualityRule,
  DataQualityRuleCreate,
  DataQualityRuleUpdate,
  DataQualityViolation,
  DataQualityValidateResponse,
  DataTag,
  DataTagCreate,
  DataTagUpdate,
  DownstreamAsset,
  DownstreamAssetCreate,
  DownstreamAssetSummary,
  DownstreamAssetUpdate,
  GatewayQueryReference,
  ImpactScanResponse,
  PersonaTagRestriction,
  PersonaTagRestrictionRequest,
  HierarchyLevelCreate,
  HierarchyLevelUpdate,
  HierarchyLevel,
  HierarchyPreviewResponse,
  HierarchyReorderRequest,
  Join,
  JoinCreate,
  LineageGraph,
  LoginRequest,
  Measure,
  MeasureCreate,
  ValidateMeasureExpressionRequest,
  ValidateMeasureExpressionResponse,
  Model,
  ModelCreate,
  ModelUpdate,
  ModelMetrics,
  TableAttribute,
  TablePreviewResponse,
  ModelColumnUpdate,
  ModelTable,
  ModelTableCreate,
  ModelTableUpdate,
  OptimizeRunRequest,
  OptimizeRunResponse,
  PaginatedQueryLogs,
  ProfiledTable,
  Project,
  ProjectCreate,
  ProjectUpdate,
  QueryMissLog,
  PocketCreate,
  PocketDefinition,
  PocketDryRunResponse,
  PocketRefreshPolicy,
  PocketRefreshRun,
  PocketUpdate,
  PocketValidateResponse,
  RefreshPolicy,
  RefreshPolicyCreate,
  RefreshRun,
  RowSecurityRule,
  RowSecurityRuleCreate,
  RowSecurityRuleUpdate,
  RowSecuritySimulateRequest,
  RowSecuritySimulateResponse,
  SchedulerTriggerRequest,
  SLAConfig,
  SLAConfigCreate,
  Source,
  SourceCreate,
  Target,
  TargetCreate,
  ExecuteResponse as QueryExecuteResponse,
  ExplainResponse as QueryExplainResponse,
  GlossaryBootstrapResponse,
  GlossaryEntry,
  GlossaryEntryCreate,
  GlossaryEntryUpdate,
  QueryRouterRequest,
  DrillThroughRequest,
  DrillThroughResponse,
  DrillOptionsRequest,
  DrillOptionsResponse,
  DrillThroughSet,
  DrillThroughSetUpdate,
  DrillJoinPathsResponse,
  Persona,
  PersonaCreate,
  PersonaUpdate,
  PersonaResolution,
  Tenant,
  TenantCreate,
  TenantUpdate,
  LoginResponse,
  TriggerRefreshResponse,
  User,
  AccessSupersedePreflightResponse,
  UserAccessBinding,
  UserAccessBindingCreate,
  ValidateResponse as QueryValidateResponse,
  UserDefinedAttribute,
  UserDefinedAttributeCreate,
  UserDefinedAttributeFunctionOption,
  UserDefinedAttributeUpdate,
  UserDefinedAttributeValidateRequest,
  UserDefinedAttributeValidateResponse,
  UserCreate,
  UserPasswordReset,
  UserUpdate,
  PersonalAccessToken,
  PersonalAccessTokenCreate,
  PersonalAccessTokenCreateResponse,
  NotificationRoute,
  NotificationRouteCreate,
  NotificationRouteUpdate,
  NotificationDelivery,
  EventTypeOption,
  FieldCompatibilityResponse,
  QueryLogFilters,
} from "./types";

// ---------------------------------------------------------------------------
// Axios instance
// ---------------------------------------------------------------------------

import {
  issuerBaseUrl,
  modelServiceBaseUrl,
  optimizerBaseUrl,
  queryRouterBaseUrl,
  schedulerBaseUrl,
} from "./apiBase";
import { isApplyingHistory } from "../store/historyApplyGuard";
// F-026-02: the store must be imported statically so markDirty runs
// synchronously in the response interceptor — a dynamic import().then()
// defers the revision bump by a microtask, causing pushAction (which reads
// currentRevision synchronously in the mutation's onSuccess) to snapshot a
// stale value and break edit→undo→clean. The original dynamic import was a
// cycle guard (store→client→store), but historyApplyGuard.ts broke that
// cycle by extracting the guard module.
import { useModelEditorStore } from "../store/useModelEditorStore";

function getCsrfToken(): string | undefined {
  return document.cookie
    .split("; ")
    .find((row) => row.startsWith("csrf_token="))
    ?.split("=")[1];
}

function csrfInterceptor(config: import("axios").InternalAxiosRequestConfig) {
  const csrf = getCsrfToken();
  if (csrf) config.headers["X-CSRF-Token"] = csrf;
  config.headers["X-Requested-With"] = "TessalliteSPA";
  config.headers["X-Tessallite-Client"] = "spa";
  return config;
}

const api: AxiosInstance = axios.create({
  baseURL: modelServiceBaseUrl(),
  headers: { "Content-Type": "application/json" },
  withCredentials: true,
});

api.interceptors.request.use(csrfInterceptor);

// Match any model-scoped write — POST/PUT/PATCH/DELETE under
// /api/v1/projects/{p}/models/{m}/...  — and fire markDirty in the
// editor store. Save / Deploy / Revert (under .../versions, .../deploy,
// .../export, .../import) are explicitly excluded so they can call
// markClean themselves with the new version pointers.
const MODEL_WRITE_RE =
  /\/api\/v1\/projects\/[^/]+\/models\/(?!snapshot-export|snapshot-import)([^/]+)(\/(?!versions|deploy|undeploy|export|import|snapshot-export|snapshot-import).*)?$/;

// F-026-11: a bare PATCH to the model itself (no sub-resource path segment) is
// used by both content edits AND non-content writes — the debounced canvas
// layout flush (`canvas_layout`) and the runtime "Model enabled" switch
// (`status`). Those two write a purely cosmetic / operational state that does
// NOT change the model's semantic content, so they must not mark the model
// "edited" (which would force a pointless new version and block Deploy). When
// the bare-model PATCH body contains only keys from this set, skip markDirty.
const NON_CONTENT_MODEL_KEYS = new Set(["canvas_layout", "status"]);

export function isNonContentModelWrite(
  method: string,
  hasSubResourcePath: boolean,
  rawBody: unknown,
): boolean {
  // Only the bare-model PATCH can be layout/status-only. Any sub-resource
  // write (dimensions, measures, joins, …) is always content.
  if (method !== "PATCH" || hasSubResourcePath) return false;
  let body: unknown = rawBody;
  if (typeof body === "string") {
    try {
      body = JSON.parse(body);
    } catch {
      return false; // can't classify — treat as content (safe default)
    }
  }
  if (!body || typeof body !== "object") return false;
  const keys = Object.keys(body as Record<string, unknown>);
  if (keys.length === 0) return false;
  return keys.every((k) => NON_CONTENT_MODEL_KEYS.has(k));
}

api.interceptors.response.use(
  (res) => {
    try {
      const method = (res.config?.method || "").toUpperCase();
      const url = res.config?.url || "";
      if (
        method !== "GET" &&
        method !== "HEAD" &&
        res.status >= 200 &&
        res.status < 300
      ) {
        const m = MODEL_WRITE_RE.exec(url);
        // F-026-02: when undo/redo replays an API write, the history hook sets
        // the content revision directly (delta-based). The interceptor must not
        // ALSO bump it — that would leave the model dirty after undoing the
        // only edit back to the saved baseline.
        const applyingHistory = isApplyingHistory();
        if (m && !applyingHistory && !isNonContentModelWrite(method, Boolean(m[2]), res.config?.data)) {
          const writtenModelId = m[1];
          // F-026-02: mark dirty SYNCHRONOUSLY so the currentRevision bump
          // lands before any mutation onSuccess (which records the history
          // entry with revisionAfter = currentRevision). A deferred
          // microtask (dynamic import .then) races the onSuccess and can
          // snapshot a stale revision, breaking edit→undo→clean.
          const openModelId = useModelEditorStore.getState().modelId;
          if (openModelId && openModelId === writtenModelId) {
            useModelEditorStore.getState().markDirty();
          }
        }
      }
    } catch {
      // never let dirty tracking break a successful API call
    }
    return res;
  },
  (err) => {
    const url = err.config?.url ?? "";
    const isLoginRequest = url.includes("/auth/login") || url.includes("/auth/system/login");
    if (err.response?.status === 401 && !isLoginRequest) {
      api.post("/api/v1/auth/logout").catch((e: unknown) => console.warn("Logout on 401 failed:", e));
      import("./auth").then(({ dispatchSessionExpired }) =>
        dispatchSessionExpired(),
      );
    }
    return Promise.reject(err);
  }
);

const schedulerApi = axios.create({
  baseURL: schedulerBaseUrl(),
  headers: { "Content-Type": "application/json" },
  withCredentials: true,
});
schedulerApi.interceptors.request.use(csrfInterceptor);

const optimizerApi = axios.create({
  baseURL: optimizerBaseUrl(),
  headers: { "Content-Type": "application/json" },
  withCredentials: true,
});
optimizerApi.interceptors.request.use(csrfInterceptor);

const queryRouterApi = axios.create({
  baseURL: queryRouterBaseUrl(),
  headers: { "Content-Type": "application/json" },
  withCredentials: true,
});
queryRouterApi.interceptors.request.use(csrfInterceptor);

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export const authApi = {
  login: (data: LoginRequest) =>
    api.post<LoginResponse>("/api/v1/auth/login", data).then((r) => r.data),
  systemLogin: (data: { email: string; password: string }) =>
    api.post<LoginResponse>("/api/v1/auth/system/login", data).then((r) => r.data),
  logout: () => api.post("/api/v1/auth/logout").then((r) => r.data),
  refresh: () =>
    api.post<LoginResponse>("/api/v1/auth/refresh").then((r) => r.data),
  bootstrapUser: (tenantId: string, data: UserCreate) =>
    api
      .post(
        `/api/v1/auth/users/bootstrap?tenant_id=${encodeURIComponent(tenantId)}`,
        data,
      )
      .then((r) => r.data),
  createUser: (data: UserCreate) =>
    api.post("/api/v1/auth/users", data).then((r) => r.data),
  createTenantUser: (tenantId: string, data: UserCreate) =>
    api
      .post<User>(
        `/api/v1/auth/users?tenant_id=${encodeURIComponent(tenantId)}`,
        data,
      )
      .then((r) => r.data),
  listTenantUsers: (tenantId: string) =>
    api
      .get<User[]>(`/api/v1/auth/users?tenant_id=${encodeURIComponent(tenantId)}`)
      .then((r) => r.data),
  updateTenantUser: (tenantId: string, userId: string, data: UserUpdate) =>
    api
      .patch<User>(
        `/api/v1/auth/users/${userId}?tenant_id=${encodeURIComponent(tenantId)}`,
        data,
      )
      .then((r) => r.data),
  resetTenantUserPassword: (
    tenantId: string,
    userId: string,
    data: UserPasswordReset,
  ) =>
    api
      .post<User>(
        `/api/v1/auth/users/${userId}/reset-password?tenant_id=${encodeURIComponent(tenantId)}`,
        data,
      )
      .then((r) => r.data),
  deleteTenantUser: (tenantId: string, userId: string) =>
    api.delete(`/api/v1/auth/users/${userId}?tenant_id=${encodeURIComponent(tenantId)}`),
  me: () => api.get<User>("/api/v1/auth/users/me").then((r) => r.data),
  completeOnboarding: () =>
    api.post<User>("/api/v1/auth/users/me/complete-onboarding").then((r) => r.data),
};

// Personal Access Tokens (Bug-7314): a logged-in user manages their own PATs
// for BI-client auth. The plaintext token is returned ONLY by create().
export const patApi = {
  list: () =>
    api
      .get<PersonalAccessToken[]>("/api/v1/auth/tokens")
      .then((r) => r.data),
  create: (data: PersonalAccessTokenCreate) =>
    api
      .post<PersonalAccessTokenCreateResponse>("/api/v1/auth/tokens", data)
      .then((r) => r.data),
  revoke: (tokenId: string) =>
    api.delete(`/api/v1/auth/tokens/${encodeURIComponent(tokenId)}`),
};

// Bug-8164: the nested manager ``status()`` shape carried inside the GET
// /admin/license response. When a persisted licence was rejected the backend puts
// the fail-closed state here (``license_state``) alongside the stable
// machine-readable ``error_code`` taxonomy token — so the card can render a
// code-specific reason for a stored bad licence, not just a generic banner. The
// code lives ONLY here (nested), never copied to the top-level response.
export type LicenseStatusDetail = {
  edition?: string;
  activated?: boolean;
  enforcement?: boolean;
  license_id?: string;
  expires_at?: string | null;
  license_state?: string; // "invalid" | "expired" | "manager_load_failed"
  // e.g. "license_expired" | "invalid_signature" | "unknown_key_id" |
  // "malformed_license" | "unsupported_algorithm"
  error_code?: string;
  load_error?: string;
};

export type LicenseManagerStatus = {
  edition: string | null;
  status: LicenseStatusDetail;
  entitlements: Record<string, unknown>;
  enforcement_enabled: boolean;
  has_license: boolean;
};

export const adminApi = {
  migrateTenant: (tenantId: string) =>
    api.post(`/api/v1/admin/migrate/tenant/${encodeURIComponent(tenantId)}`).then((r) => r.data),
  // License manager (system-admin only).
  licenseStatus: () =>
    api.get<LicenseManagerStatus>("/api/v1/admin/license").then((r) => r.data),
  installLicense: (doc: unknown) =>
    api
      .post<{ status: string; license: LicenseManagerStatus }>("/api/v1/admin/license", doc)
      .then((r) => r.data),
  uninstallLicense: () =>
    api
      .delete<{ status: string; license: LicenseManagerStatus; message?: string }>(
        "/api/v1/admin/license",
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// System Settings
// ---------------------------------------------------------------------------

export type SystemSettingItem = {
  key: string;
  section: string;
  type: string;
  description: string;
  restart_required: boolean;
  default: unknown;
  value: unknown;
  env_var: string | null;
  sensitive_display: boolean;
  label?: string | null;
  ui_help?: string | null;
  ui_group?: string | null;
  ui_control?: string | null;
  ui_choices?: unknown[] | null;
  unit?: string | null;
};

export type RestartPendingItem = {
  setting_key: string;
  written_at: string | null;
  written_by: string | null;
};

export type BootstrapItem = {
  name: string;
  value: string;
  sensitive: boolean;
  description: string;
};

export const systemSettingsApi = {
  list: () =>
    api
      .get<{ items: SystemSettingItem[] }>("/api/v1/system/settings")
      .then((r) => r.data.items),
  put: (key: string, value: unknown) =>
    api
      .put<{ status: string; key: string; restart_required: boolean }>(
        `/api/v1/system/settings/${encodeURIComponent(key)}`,
        { value },
      )
      .then((r) => r.data),
  restartPending: () =>
    api
      .get<RestartPendingItem[]>("/api/v1/system/settings/restart-pending")
      .then((r) => r.data),
  clearRestartPending: () =>
    api
      .post("/api/v1/system/settings/restart-pending/clear")
      .then((r) => r.data),
  bootstrap: () =>
    api
      .get<BootstrapItem[]>("/api/v1/system/settings/bootstrap")
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Edition / licensing (read-only; the UI badge + current/max display)
// ---------------------------------------------------------------------------

export type EditionStatus = {
  edition: string; // "community" | "enterprise" | "unactivated"
  activated: boolean;
  enforcement?: boolean;
  license_id?: string;
  expires_at?: string | null;
  // Bug-7680: when a licence document IS installed but the verifier rejected it
  // (expired / untrusted signature), the backend reports the fail-closed state
  // with ``license_state: "invalid"`` (see model-service _InvalidLicenseManager).
  // The UI must render this distinctly from both "activated" and the plain
  // "unactivated / nothing installed" state, so an invalid licence is not
  // mistaken for a normal one.
  license_state?: string; // e.g. "invalid"
};

export type EditionLimits = {
  edition?: string;
  entitlements: Record<string, unknown>;
  usage?: {
    models?: number | null;
    users?: number | null;
    tenants?: number | null;
    projects?: number | null;
  };
};

export const editionApi = {
  getEdition: () =>
    api.get<EditionStatus>("/api/v1/edition").then((r) => r.data),
  getLimits: () =>
    api.get<EditionLimits>("/api/v1/limits").then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Advisory / update feed (read-only; served by the public issuer service)
// ---------------------------------------------------------------------------

export type Advisory = {
  id: string;
  published?: string;
  severity?: string; // "info" | "low" | "medium" | "high" | "critical"
  title: string;
  summary?: string;
  fixed_in?: string;
  link?: string;
};

type AdvisoryFeed = { advisories?: Advisory[] };

// The issuer is external and unauthenticated: use a bare axios call (no
// credentials, no CSRF) with a short timeout so an unreachable feed degrades
// gracefully instead of hanging the UI.
export const advisoriesApi = {
  list: (): Promise<Advisory[]> =>
    axios
      .get<AdvisoryFeed>(`${issuerBaseUrl()}/advisories`, {
        withCredentials: false,
        timeout: 8000,
      })
      .then((r) => r.data?.advisories ?? []),
};

// ---------------------------------------------------------------------------
// Project settings (registry-driven per-project keys)
// ---------------------------------------------------------------------------

export type ProjectSettingItem = {
  key: string;
  section: string;
  type: string;
  description: string;
  own_value: unknown;
  effective_value: unknown;
  label?: string | null;
  ui_help?: string | null;
  ui_group?: string | null;
  ui_control?: string | null;
  ui_choices?: unknown[] | null;
  unit?: string | null;
};

export const projectSettingsApi = {
  list: (projectId: string) =>
    api
      .get<{ project_id: string; items: ProjectSettingItem[] }>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/settings`,
      )
      .then((r) => r.data.items),
  put: (projectId: string, key: string, value: unknown) =>
    api
      .put<{ status: string; project_id: string; key: string }>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/settings/${encodeURIComponent(key)}`,
        { value },
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Tenants
// ---------------------------------------------------------------------------

export const tenantsApi = {
  list: () => api.get<Tenant[]>("/api/v1/tenants").then((r) => r.data),
  me: () => api.get<Tenant>("/api/v1/tenants/me").then((r) => r.data),
  create: (data: TenantCreate) =>
    api.post<Tenant>("/api/v1/tenants", data).then((r) => r.data),
  update: (tenantId: string, data: TenantUpdate) =>
    api
      .patch<Tenant>(`/api/v1/tenants/${encodeURIComponent(tenantId)}`, data)
      .then((r) => r.data),
  delete: (tenantId: string) =>
    api.delete(`/api/v1/tenants/${encodeURIComponent(tenantId)}`),
};

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

export const projectsApi = {
  list: () => api.get<Project[]>("/api/v1/projects").then((r) => r.data),
  get: (id: string) =>
    api.get<Project>(`/api/v1/projects/${id}`).then((r) => r.data),
  create: (data: ProjectCreate) =>
    api.post<Project>("/api/v1/projects", data).then((r) => r.data),
  update: (id: string, data: ProjectUpdate) =>
    api.patch<Project>(`/api/v1/projects/${id}`, data).then((r) => r.data),
  delete: (id: string) => api.delete(`/api/v1/projects/${id}`),
};

export const accessApi = {
  list: (projectId: string) =>
    api
      .get<UserAccessBinding[]>(`/api/v1/projects/${projectId}/access`)
      .then((r) => r.data),
  // Bug-8101: `supersede` confirms the Modeller-supersedes-Model-viewer rule.
  // The server rejects (409 "modeller_supersedes_model_viewer") an overlapping
  // grant unless supersede=true; the UI first calls preflight() to decide
  // whether to show the confirmation.
  grant: (
    projectId: string,
    data: UserAccessBindingCreate,
    supersede = false,
  ) =>
    api
      .post<UserAccessBinding>(
        `/api/v1/projects/${projectId}/access${supersede ? "?supersede=true" : ""}`,
        data,
      )
      .then((r) => r.data),
  preflight: (projectId: string, data: UserAccessBindingCreate) =>
    api
      .post<AccessSupersedePreflightResponse>(
        `/api/v1/projects/${projectId}/access/preflight`,
        data,
      )
      .then((r) => r.data),
  revoke: (projectId: string, bindingId: string) =>
    api.delete(`/api/v1/projects/${projectId}/access/${bindingId}`),
};

// ---------------------------------------------------------------------------
// Connections
// ---------------------------------------------------------------------------

export const connectionsApi = {
  list: (projectId: string) =>
    api
      .get<Connection[]>(`/api/v1/projects/${projectId}/connections`)
      .then((r) => r.data),
  create: (projectId: string, data: ConnectionCreate) =>
    api
      .post<Connection>(`/api/v1/projects/${projectId}/connections`, data)
      .then((r) => r.data),
  update: (projectId: string, connId: string, data: Partial<ConnectionCreate>) =>
    api
      .patch<Connection>(`/api/v1/projects/${projectId}/connections/${connId}`, data)
      .then((r) => r.data),
  test: (projectId: string, connId: string) =>
    api
      .post(`/api/v1/projects/${projectId}/connections/${connId}/test`)
      .then((r) => r.data),
  testEdit: (
    projectId: string,
    connId: string,
    data: {
      credentials?: Record<string, unknown>;
      config?: Record<string, unknown>;
    },
  ) =>
    api
      .post(`/api/v1/projects/${projectId}/connections/${connId}/test_edit`, data)
      .then((r) => r.data),
  testDraft: (
    projectId: string,
    data: {
      connection_type: string;
      credentials: Record<string, unknown>;
      config?: Record<string, unknown>;
    },
  ) =>
    api
      .post(`/api/v1/projects/${projectId}/connections/test`, data)
      .then((r) => r.data),
  discoverTables: (projectId: string, connId: string, schemaFilter?: string) =>
    api
      .get<
        | { tables: Array<{ schema: string; table: string; type: string }>; truncated?: boolean }
        | Array<{ schema: string; table: string; type: string }>
      >(
        `/api/v1/projects/${projectId}/connections/${connId}/tables${
          schemaFilter ? `?schema_filter=${encodeURIComponent(schemaFilter)}` : ""
        }`
      )
      .then((r) => r.data),
  discoverColumns: (projectId: string, connId: string, schema: string, table: string) =>
    api
      .get<DiscoveredColumn[]>(
        `/api/v1/projects/${projectId}/connections/${connId}/columns?schema=${encodeURIComponent(schema)}&table=${encodeURIComponent(table)}`
      )
      .then((r) => r.data),
  delete: (projectId: string, connId: string) =>
    api.delete(`/api/v1/projects/${projectId}/connections/${connId}`),
  profileTables: (
    projectId: string,
    connId: string,
    tables: Array<{ schema: string; table: string }>,
  ) =>
    api
      .post<ProfiledTable[]>(
        `/api/v1/projects/${projectId}/connections/${connId}/profile`,
        { tables },
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------

export const alertsApi = {
  list: (
    projectId: string,
    modelId: string,
    opts: {
      severity?: string;
      category?: string;
      include_resolved?: boolean;
      include_dismissed?: boolean;
      limit?: number;
      offset?: number;
    } = {},
  ) => {
    const p = new URLSearchParams();
    if (opts.severity) p.set("severity", opts.severity);
    if (opts.category) p.set("category", opts.category);
    if (opts.include_resolved) p.set("include_resolved", "true");
    if (opts.include_dismissed) p.set("include_dismissed", "true");
    if (opts.limit != null) p.set("limit", String(opts.limit));
    if (opts.offset != null) p.set("offset", String(opts.offset));
    const qs = p.toString();
    return api
      .get<import("./types").ModelAlert[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/alerts${qs ? "?" + qs : ""}`,
      )
      .then((r) => r.data);
  },
  count: (
    projectId: string,
    modelId: string,
    opts: {
      severity?: string;
      category?: string;
      include_resolved?: boolean;
      include_dismissed?: boolean;
    } = {},
  ) => {
    const p = new URLSearchParams();
    if (opts.severity) p.set("severity", opts.severity);
    if (opts.category) p.set("category", opts.category);
    if (opts.include_resolved) p.set("include_resolved", "true");
    if (opts.include_dismissed) p.set("include_dismissed", "true");
    const qs = p.toString();
    return api
      .get<import("./types").ModelAlertCount>(
        `/api/v1/projects/${projectId}/models/${modelId}/alerts/count${qs ? "?" + qs : ""}`,
      )
      .then((r) => r.data);
  },
  dismiss: (projectId: string, modelId: string, alertId: string) =>
    api
      .post<import("./types").ModelAlert>(
        `/api/v1/projects/${projectId}/models/${modelId}/alerts/${alertId}/dismiss`,
      )
      .then((r) => r.data),
  revalidate: (projectId: string, modelId: string) =>
    api
      .post<import("./types").ModelRevalidationReport>(
        `/api/v1/projects/${projectId}/models/${modelId}/revalidate`,
      )
      .then((r) => r.data),
};

export const modelsApi = {
  list: (projectId: string) =>
    api
      .get<Model[]>(`/api/v1/projects/${projectId}/models`)
      .then((r) => r.data),
  get: (projectId: string, modelId: string) =>
    api
      .get<Model>(`/api/v1/projects/${projectId}/models/${modelId}`)
      .then((r) => r.data),
  create: (projectId: string, data: ModelCreate) =>
    api
      .post<Model>(`/api/v1/projects/${projectId}/models`, data)
      .then((r) => r.data),
  update: (projectId: string, modelId: string, data: ModelUpdate) =>
    api
      .patch<Model>(`/api/v1/projects/${projectId}/models/${modelId}`, data)
      .then((r) => r.data),
  delete: (projectId: string, modelId: string) =>
    api.delete(`/api/v1/projects/${projectId}/models/${modelId}`),
  lineage: (projectId: string, modelId: string) =>
    api
      .get<LineageGraph>(
        `/api/v1/projects/${projectId}/models/${modelId}/lineage`
      )
      .then((r) => r.data),
  export: (projectId: string, modelId: string) =>
    api
      .get(`/api/v1/projects/${projectId}/models/${modelId}/export`)
      .then((r) => r.data),
  metrics: (projectId: string, modelId: string, windowHours = 24) =>
    api
      .get<ModelMetrics>(
        `/api/v1/projects/${projectId}/models/${modelId}/metrics?window_hours=${windowHours}`
      )
      .then((r) => r.data),
  bulkRenameAttributes: (
    projectId: string,
    modelId: string,
    renames: Array<{ type: string; id: string; name: string }>,
  ) =>
    api
      .post<{ renamed: number }>(
        `/api/v1/projects/${projectId}/models/${modelId}/bulk-rename-attributes`,
        { renames },
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Sources / Targets
// ---------------------------------------------------------------------------

export const sourcesApi = {
  list: (projectId: string, modelId: string, signal?: AbortSignal) =>
    api
      .get<Source[]>(`/api/v1/projects/${projectId}/models/${modelId}/sources`, { signal })
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: SourceCreate) =>
    api
      .post<Source>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources`,
        data
      )
      .then((r) => r.data),
  update: (projectId: string, modelId: string, sourceId: string, data: Partial<SourceCreate>) =>
    api
      .patch<Source>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}`,
        data
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, sourceId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}`
    ),
};

export const calendarApi = {
  list: (projectId: string, modelId: string, sourceId: string) =>
    api
      .get<CalendarTable[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/calendars`,
      )
      .then((r) => r.data),
  // Bug-5920: backend-computed availability per calendar type, replacing
  // the hardcoded HIJRI_AVAILABLE frontend flag.
  types: (projectId: string, modelId: string, sourceId: string) =>
    api
      .get<CalendarTypeAvailability[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/calendars/types`,
      )
      .then((r) => r.data),
  script: (
    projectId: string,
    modelId: string,
    sourceId: string,
    body: CalendarScriptRequest,
  ) =>
    api
      .post<CalendarScriptResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/calendars/script`,
        body,
      )
      .then((r) => r.data),
  autoCreate: (
    projectId: string,
    modelId: string,
    sourceId: string,
    body: CalendarAutoCreateRequest,
  ) =>
    api
      .post<CalendarTable>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/calendars/auto-create`,
        body,
      )
      .then((r) => r.data),
  bind: (
    projectId: string,
    modelId: string,
    sourceId: string,
    body: CalendarBindRequest,
  ) =>
    api
      .post<CalendarTable>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/calendars/bind`,
        body,
      )
      .then((r) => r.data),
  update: (
    projectId: string,
    modelId: string,
    sourceId: string,
    calendarId: string,
    body: CalendarUpdateRequest,
  ) =>
    api
      .put<CalendarTable>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/calendars/${calendarId}`,
        body,
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, sourceId: string, calendarId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/calendars/${calendarId}`,
    ),
  // F-016-23: compare the calendar's date range against a fact table's actual
  // data range so the UI can warn before out-of-range fact rows drop to NULL
  // period values.
  coverage: (
    projectId: string,
    modelId: string,
    sourceId: string,
    calendarId: string,
    factTable: string,
    factDateColumn: string,
  ) =>
    api
      .get<CalendarCoverageResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/calendars/${calendarId}/coverage`,
        { params: { fact_table: factTable, fact_date_column: factDateColumn } },
      )
      .then((r) => r.data),
};

export const modelTablesApi = {
  list: (projectId: string, modelId: string, sourceId: string, signal?: AbortSignal) =>
    api
      .get<ModelTable[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/tables`,
        { signal },
      )
      .then((r) => r.data),
  create: (
    projectId: string,
    modelId: string,
    sourceId: string,
    data: ModelTableCreate
  ) =>
    api
      .post<ModelTable>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/tables`,
        data
      )
      .then((r) => r.data),
  update: (
    projectId: string,
    modelId: string,
    sourceId: string,
    tableId: string,
    data: ModelTableUpdate
  ) =>
    api
      .patch<ModelTable>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/tables/${tableId}`,
        data
      )
      .then((r) => r.data),
  delete: (
    projectId: string,
    modelId: string,
    sourceId: string,
    tableId: string
  ) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/tables/${tableId}`
    ),
  analyze: (
    projectId: string,
    modelId: string,
    sourceId: string,
    tableId: string
  ) =>
    api
      .post<import("./types").TableAnalysis>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/tables/${tableId}/analyze`
      )
      .then((r) => r.data),
  renamePreview: (
    projectId: string,
    modelId: string,
    sourceId: string,
    tableId: string,
    newAlias: string,
  ) =>
    api
      .get<import("./types").RenamePreviewItem[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/tables/${tableId}/rename-preview`,
        { params: { new_alias: newAlias } },
      )
      .then((r) => r.data),
  preview: (
    projectId: string,
    modelId: string,
    tableId: string,
    page: number,
    pageSize: number,
    count = false,
  ) =>
    api
      .get<TablePreviewResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/tables/${tableId}/preview`,
        { params: { page, page_size: pageSize, count } },
      )
      .then((r) => r.data),
};

export const tableAttributesApi = {
  list: (projectId: string, modelId: string, tableId: string) =>
    api
      .get<TableAttribute[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/tables/${tableId}/attributes`
      )
      .then((r) => r.data),
  syncColumns: (
    projectId: string,
    modelId: string,
    tableId: string,
    columns: Array<{
      column_name: string;
      data_type: string;
      is_nullable: boolean;
      /** Omit when unknown; the API leaves the stored flag alone (Bug-8618). */
      is_primary_key?: boolean;
    }>,
  ) =>
    api.post(
      `/api/v1/projects/${projectId}/models/${modelId}/tables/${tableId}/sync-columns`,
      columns,
    ),
  updateColumn: (
    projectId: string,
    modelId: string,
    tableId: string,
    columnId: string,
    data: ModelColumnUpdate,
  ) =>
    api
      .patch<TableAttribute>(
        `/api/v1/projects/${projectId}/models/${modelId}/tables/${tableId}/columns/${columnId}`,
        data,
      )
      .then((r) => r.data),
  delete: (
    projectId: string,
    modelId: string,
    tableId: string,
    attributeId: string,
    kind: "physical" | "user_defined",
  ) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/tables/${tableId}/attributes/${attributeId}?kind=${encodeURIComponent(kind)}`
    ),
};

export const targetsApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<Target[]>(`/api/v1/projects/${projectId}/models/${modelId}/targets`)
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: TargetCreate) =>
    api
      .post<Target>(
        `/api/v1/projects/${projectId}/models/${modelId}/targets`,
        data
      )
      .then((r) => r.data),
  update: (projectId: string, modelId: string, targetId: string, data: Partial<TargetCreate>) =>
    api
      .patch<Target>(
        `/api/v1/projects/${projectId}/models/${modelId}/targets/${targetId}`,
        data
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, targetId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/targets/${targetId}`
    ),
};

// ---------------------------------------------------------------------------
// Dimensions / Measures
// ---------------------------------------------------------------------------

export const hierarchiesApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<Hierarchy[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/hierarchies`
      )
      .then((r) => r.data),
  get: (projectId: string, modelId: string, hierarchyId: string) =>
    api
      .get<HierarchyDetail>(
        `/api/v1/projects/${projectId}/models/${modelId}/hierarchies/${hierarchyId}`
      )
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: HierarchyCreate) =>
    api
      .post<Hierarchy>(
        `/api/v1/projects/${projectId}/models/${modelId}/hierarchies`,
        data
      )
      .then((r) => r.data),
  update: (
    projectId: string,
    modelId: string,
    hierarchyId: string,
    data: HierarchyUpdate,
  ) =>
    api
      .put<HierarchyDetail>(
        `/api/v1/projects/${projectId}/models/${modelId}/hierarchies/${hierarchyId}`,
        data,
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, hierarchyId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/hierarchies/${hierarchyId}`
    ),
  listLevels: (projectId: string, modelId: string, hierarchyId: string) =>
    api
      .get<HierarchyLevel[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/hierarchies/${hierarchyId}/levels`
      )
      .then((r) => r.data),
  createLevel: (
    projectId: string,
    modelId: string,
    hierarchyId: string,
    data: HierarchyLevelCreate,
  ) =>
    api
      .post<HierarchyLevel>(
        `/api/v1/projects/${projectId}/models/${modelId}/hierarchies/${hierarchyId}/levels`,
        data
      )
      .then((r) => r.data),
  updateLevel: (
    projectId: string,
    modelId: string,
    hierarchyId: string,
    levelId: string,
    data: HierarchyLevelUpdate,
  ) =>
    api
      .put<HierarchyLevel>(
        `/api/v1/projects/${projectId}/models/${modelId}/hierarchies/${hierarchyId}/levels/${levelId}`,
        data,
      )
      .then((r) => r.data),
  deleteLevel: (
    projectId: string,
    modelId: string,
    hierarchyId: string,
    levelId: string,
  ) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/hierarchies/${hierarchyId}/levels/${levelId}`
    ),
  reorderLevels: (
    projectId: string,
    modelId: string,
    hierarchyId: string,
    data: HierarchyReorderRequest,
  ) =>
    api
      .put<HierarchyLevel[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/hierarchies/${hierarchyId}/levels/reorder`,
        data
      )
      .then((r) => r.data),
  generateDate: (
    projectId: string,
    modelId: string,
    data: HierarchyGenerateDateRequest,
  ) =>
    api
      .post<HierarchyGeneratedResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/hierarchies/generate-date`,
        data
      )
      .then((r) => r.data),
  generateSegment: (
    projectId: string,
    modelId: string,
    data: HierarchyGenerateSegmentRequest,
  ) =>
    api
      .post<HierarchyGeneratedResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/hierarchies/generate-segment`,
        data
      )
      .then((r) => r.data),
  listUnassignedDates: (projectId: string, modelId: string) =>
    api
      .get<UnassignedDateColumn[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/columns/unassigned-dates`
      )
      .then((r) => r.data),
  batchCreateDate: (
    projectId: string,
    modelId: string,
    data: HierarchyBatchDateRequest,
  ) =>
    api
      .post<HierarchyBatchDateResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/hierarchies/batch-date`,
        data
      )
      .then((r) => r.data),
  preview: (
    projectId: string,
    modelId: string,
    hierarchyId: string,
    params?: { sample_size?: number; expand_level?: number; parent_key?: string },
  ) => {
    const search = new URLSearchParams();
    if (params?.sample_size != null) search.set("sample_size", String(params.sample_size));
    if (params?.expand_level != null) search.set("expand_level", String(params.expand_level));
    if (params?.parent_key) search.set("parent_key", params.parent_key);
    return api
      .get<HierarchyPreviewResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/hierarchies/${hierarchyId}/preview${
          search.toString() ? `?${search.toString()}` : ""
        }`
      )
      .then((r) => r.data);
  },
  health: (projectId: string, modelId: string, probeMembers = false) =>
    api
      .get<HierarchyHealthStatus[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/hierarchy-health`,
        { params: probeMembers ? { probe_members: true } : undefined },
      )
      .then((r) => r.data),
  grainSuggestions: (projectId: string, modelId: string) =>
    api
      .get<GrainSuggestion[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/hierarchy-health/grain-suggestions`,
      )
      .then((r) => r.data),
};

export const dataQualityApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<DataQualityRule[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/data-quality-rules`
      )
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: DataQualityRuleCreate) =>
    api
      .post<DataQualityRule>(
        `/api/v1/projects/${projectId}/models/${modelId}/data-quality-rules`,
        data
      )
      .then((r) => r.data),
  update: (projectId: string, modelId: string, ruleId: string, data: DataQualityRuleUpdate) =>
    api
      .put<DataQualityRule>(
        `/api/v1/projects/${projectId}/models/${modelId}/data-quality-rules/${ruleId}`,
        data
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, ruleId: string) =>
    api.delete(`/api/v1/projects/${projectId}/models/${modelId}/data-quality-rules/${ruleId}`),
  validate: (projectId: string, modelId: string) =>
    api
      .post<DataQualityValidateResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/data-quality-rules/validate`
      )
      .then((r) => r.data),
  listViolations: (projectId: string, modelId: string, ruleId: string, limit = 50) =>
    api
      .get<DataQualityViolation[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/data-quality-rules/${ruleId}/violations?limit=${limit}`
      )
      .then((r) => r.data),
  clearViolations: (projectId: string, modelId: string, ruleId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/data-quality-rules/${ruleId}/violations`
    ),
  aggregateViolationSummary: (projectId: string, modelId: string) =>
    api
      .get<Record<string, number>>(
        `/api/v1/projects/${projectId}/models/${modelId}/data-quality-rules/aggregate-violations`
      )
      .then((r) => r.data),
  pocketViolationSummary: (projectId: string, modelId: string) =>
    api
      .get<Record<string, number>>(
        `/api/v1/projects/${projectId}/models/${modelId}/data-quality-rules/pocket-violations`
      )
      .then((r) => r.data),
};

export const dimensionsApi = {
  list: (projectId: string, modelId: string, signal?: AbortSignal) =>
    api
      .get<Dimension[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/dimensions`,
        { signal },
      )
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: DimensionCreate) =>
    api
      .post<Dimension>(
        `/api/v1/projects/${projectId}/models/${modelId}/dimensions`,
        data
      )
      .then((r) => r.data),
  update: (
    projectId: string,
    modelId: string,
    dimId: string,
    data: Partial<DimensionCreate>,
  ) =>
    api
      .patch<Dimension>(
        `/api/v1/projects/${projectId}/models/${modelId}/dimensions/${dimId}`,
        data
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, dimId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/dimensions/${dimId}`
    ),
};

// Dimension attribute relationships (derived-grain routing, spec section 5.3).
// A modeller-declared key-to-detail relationship on a dimension; distinct from the
// display column. Verification status is projected by the backend (DECLARED until
// proven on deploy). Mirrors dimensionsApi's list/create/update/delete shape.
export const attributeRelationshipsApi = {
  list: (projectId: string, modelId: string, dimId: string) =>
    api
      .get<DimensionAttributeRelationship[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/dimensions/${dimId}/attribute-relationships`,
      )
      .then((r) => r.data),
  create: (
    projectId: string,
    modelId: string,
    dimId: string,
    data: DimensionAttributeRelationshipCreate,
  ) =>
    api
      .post<DimensionAttributeRelationship>(
        `/api/v1/projects/${projectId}/models/${modelId}/dimensions/${dimId}/attribute-relationships`,
        data,
      )
      .then((r) => r.data),
  update: (
    projectId: string,
    modelId: string,
    dimId: string,
    relId: string,
    data: DimensionAttributeRelationshipUpdate,
  ) =>
    api
      .patch<DimensionAttributeRelationship>(
        `/api/v1/projects/${projectId}/models/${modelId}/dimensions/${dimId}/attribute-relationships/${relId}`,
        data,
      )
      .then((r) => r.data),
  delete: (
    projectId: string,
    modelId: string,
    dimId: string,
    relId: string,
    retireAggregates?: boolean,
  ) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/dimensions/${dimId}/attribute-relationships/${relId}`,
      { params: retireAggregates ? { retire_aggregates: true } : undefined },
    ),
  validate: (
    projectId: string,
    modelId: string,
    dimId: string,
    detailColumns: Array<{ name: string; table_id: string }>,
  ) =>
    api
      .post<
        Array<{
          column: string;
          table_id?: string | null;
          is_bijection: boolean;
          reason: string;
          error?: string | null;
        }>
      >(
        `/api/v1/projects/${projectId}/models/${modelId}/dimensions/${dimId}/attribute-relationships/validate`,
        { detail_columns: detailColumns },
      )
      .then((r) => r.data),
  downstreamUsage: (
    projectId: string,
    modelId: string,
    dimId: string,
    relId: string,
  ) =>
    api
      .get<{
        linked_dimensions: Array<{ id: string; name: string }>;
        affected_aggregates: Array<{
          id: string;
          physical_table_name: string;
          grain: string[];
          status: string;
        }>;
      }>(
        `/api/v1/projects/${projectId}/models/${modelId}/dimensions/${dimId}/attribute-relationships/${relId}/downstream-usage`,
      )
      .then((r) => r.data),
};

export interface AvailableVariant {
  kind: string;
  eligible: boolean;
  reason: string | null;
  suggested_name: string;
  suggested_display_name: string;
  existing_measure_id: string | null;
}

export const measuresApi = {
  list: (projectId: string, modelId: string, signal?: AbortSignal) =>
    api
      .get<Measure[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/measures`,
        { signal },
      )
      .then((r) => r.data),
  listAvailableVariants: (
    projectId: string,
    modelId: string,
    measureId: string,
  ) =>
    api
      .get<{ variants: AvailableVariant[] }>(
        `/api/v1/projects/${projectId}/models/${modelId}/measures/${measureId}/available-variants`,
      )
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: MeasureCreate) =>
    api
      .post<Measure>(
        `/api/v1/projects/${projectId}/models/${modelId}/measures`,
        data
      )
      .then((r) => r.data),
  update: (
    projectId: string,
    modelId: string,
    measureId: string,
    data: Partial<MeasureCreate>,
  ) =>
    api
      .patch<Measure>(
        `/api/v1/projects/${projectId}/models/${modelId}/measures/${measureId}`,
        data
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, measureId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/measures/${measureId}`
    ),
  validateExpression: (
    projectId: string,
    modelId: string,
    data: ValidateMeasureExpressionRequest,
  ) =>
    api
      .post<ValidateMeasureExpressionResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/measures/validate-expression`,
        data,
      )
      .then((r) => r.data),
  getDrillThroughSet: (projectId: string, modelId: string, measureId: string) =>
    api
      .get<DrillThroughSet>(
        `/api/v1/projects/${projectId}/models/${modelId}/measures/${measureId}/drill-through-set`,
      )
      .then((r) => r.data),
  updateDrillThroughSet: (
    projectId: string,
    modelId: string,
    measureId: string,
    data: DrillThroughSetUpdate,
  ) =>
    api
      .patch<DrillThroughSet>(
        `/api/v1/projects/${projectId}/models/${modelId}/measures/${measureId}/drill-through-set`,
        data,
      )
      .then((r) => r.data),
  resetDrillThroughSet: (projectId: string, modelId: string, measureId: string) =>
    api
      .delete<DrillThroughSet>(
        `/api/v1/projects/${projectId}/models/${modelId}/measures/${measureId}/drill-through-set`,
      )
      .then((r) => r.data),
  listDrillJoinPaths: (
    projectId: string,
    modelId: string,
    measureId: string,
    sourceTableId: string,
  ) =>
    api
      .get<DrillJoinPathsResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/measures/${measureId}/drill-through-set/join-paths`,
        { params: { source_table_id: sourceTableId } },
      )
      .then((r) => r.data),
};

export const fieldCompatibilityApi = {
  get: (
    projectId: string,
    modelId: string,
    options?: {
      personaId?: string | null;
      measureIds?: string[];
      dimensionIds?: string[];
      includeHidden?: boolean;
      signal?: AbortSignal;
    },
  ) => {
    const params = new URLSearchParams();
    if (options?.personaId) params.set("persona_id", options.personaId);
    if (options?.includeHidden) params.set("include_hidden", "true");
    for (const id of options?.measureIds ?? []) params.append("measure_ids", id);
    for (const id of options?.dimensionIds ?? []) params.append("dimension_ids", id);
    return api
      .get<FieldCompatibilityResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/field-compatibility`,
        { params, signal: options?.signal },
      )
      .then((r) => r.data);
  },
};

export const userDefinedAttributesApi = {
  functionCatalog: (projectId: string, modelId: string, tableId: string) =>
    api
      .get<UserDefinedAttributeFunctionOption[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/tables/${tableId}/user-defined-attributes/function-catalog`
      )
      .then((r) => r.data),
  list: (projectId: string, modelId: string, tableId: string) =>
    api
      .get<UserDefinedAttribute[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/tables/${tableId}/user-defined-attributes`
      )
      .then((r) => r.data),
  get: (projectId: string, modelId: string, tableId: string, attrId: string) =>
    api
      .get<UserDefinedAttribute>(
        `/api/v1/projects/${projectId}/models/${modelId}/tables/${tableId}/user-defined-attributes/${attrId}`
      )
      .then((r) => r.data),
  create: (
    projectId: string,
    modelId: string,
    tableId: string,
    data: UserDefinedAttributeCreate,
  ) =>
    api
      .post<UserDefinedAttribute>(
        `/api/v1/projects/${projectId}/models/${modelId}/tables/${tableId}/user-defined-attributes`,
        data
      )
      .then((r) => r.data),
  update: (
    projectId: string,
    modelId: string,
    tableId: string,
    attrId: string,
    data: UserDefinedAttributeUpdate,
  ) =>
    api
      .put<UserDefinedAttribute>(
        `/api/v1/projects/${projectId}/models/${modelId}/tables/${tableId}/user-defined-attributes/${attrId}`,
        data
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, tableId: string, attrId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/tables/${tableId}/user-defined-attributes/${attrId}`
    ),
  validate: (
    projectId: string,
    modelId: string,
    tableId: string,
    data: UserDefinedAttributeValidateRequest,
  ) =>
    api
      .post<UserDefinedAttributeValidateResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/tables/${tableId}/user-defined-attributes/validate`,
        data
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Joins
// ---------------------------------------------------------------------------

export const joinsApi = {
  list: (projectId: string, modelId: string, signal?: AbortSignal) =>
    api
      .get<Join[]>(`/api/v1/projects/${projectId}/models/${modelId}/joins`, { signal })
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: JoinCreate) =>
    api
      .post<Join>(
        `/api/v1/projects/${projectId}/models/${modelId}/joins`,
        data
      )
      .then((r) => r.data),
  update: (projectId: string, modelId: string, joinId: string, data: Partial<JoinCreate>) =>
    api
      .patch<Join>(
        `/api/v1/projects/${projectId}/models/${modelId}/joins/${joinId}`,
        data
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, joinId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/joins/${joinId}`
    ),
};

// ---------------------------------------------------------------------------
// Aggregates
// ---------------------------------------------------------------------------

export const aggregatesApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<AggregateDefinition[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/aggregates`
      )
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: AggregateCreate) =>
    api
      .post<AggregateDefinition>(
        `/api/v1/projects/${projectId}/models/${modelId}/aggregates`,
        data
      )
      .then((r) => r.data),
  getPolicy: (projectId: string, modelId: string, aggId: string) =>
    api
      .get<RefreshPolicy>(
        `/api/v1/projects/${projectId}/models/${modelId}/aggregates/${aggId}/refresh/policy`
      )
      .then((r) => r.data),
  setPolicy: (
    projectId: string,
    modelId: string,
    aggId: string,
    data: RefreshPolicyCreate
  ) =>
    api
      .post<RefreshPolicy>(
        `/api/v1/projects/${projectId}/models/${modelId}/aggregates/${aggId}/refresh/policy`,
        data
      )
      .then((r) => r.data),
  getRuns: (projectId: string, modelId: string, aggId: string) =>
    api
      .get<RefreshRun[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/aggregates/${aggId}/refresh/runs`
      )
      .then((r) => r.data),
  getModelRuns: (projectId: string, modelId: string) =>
    api
      .get<RefreshRun[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/refresh/runs`
      )
      .then((r) => r.data),
  update: (projectId: string, modelId: string, aggId: string, data: AggregateUpdate) =>
    api
      .patch<AggregateDefinition>(
        `/api/v1/projects/${projectId}/models/${modelId}/aggregates/${aggId}`,
        data
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, aggId: string) =>
    api
      .delete(
        `/api/v1/projects/${projectId}/models/${modelId}/aggregates/${aggId}`
      )
      .then((r) => r.data),
};

export const pocketsApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<PocketDefinition[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/pockets`
      )
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: PocketCreate) =>
    api
      .post<PocketDefinition>(
        `/api/v1/projects/${projectId}/models/${modelId}/pockets`,
        data
      )
      .then((r) => r.data),
  update: (projectId: string, modelId: string, pocketId: string, data: PocketUpdate) =>
    api
      .patch<PocketDefinition>(
        `/api/v1/projects/${projectId}/models/${modelId}/pockets/${pocketId}`,
        data
      )
      .then((r) => r.data),
  refresh: (projectId: string, modelId: string, pocketId: string) =>
    api
      .post<PocketRefreshRun>(
        `/api/v1/projects/${projectId}/models/${modelId}/pockets/${pocketId}/refresh`
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, pocketId: string) =>
    api
      .delete(
        `/api/v1/projects/${projectId}/models/${modelId}/pockets/${pocketId}`
      )
      .then((r) => r.data),
  validate: (
    projectId: string,
    modelId: string,
    data: { defining_sql: string; target_id?: string | null },
  ) =>
    api
      .post<PocketValidateResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/pockets/validate`,
        data,
      )
      .then((r) => r.data),
  dryRun: (
    projectId: string,
    modelId: string,
    data: { defining_sql: string; target_id?: string | null },
  ) =>
    api
      .post<PocketDryRunResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/pockets/dry-run`,
        data,
      )
      .then((r) => r.data),
  getPolicy: (projectId: string, modelId: string, pocketId: string) =>
    api
      .get<PocketRefreshPolicy>(
        `/api/v1/projects/${projectId}/models/${modelId}/pockets/${pocketId}/refresh/policy`,
      )
      .then((r) => r.data),
  setPolicy: (
    projectId: string,
    modelId: string,
    pocketId: string,
    data: { cron_expression?: string | null; is_enabled: boolean },
  ) =>
    api
      .put<PocketRefreshPolicy>(
        `/api/v1/projects/${projectId}/models/${modelId}/pockets/${pocketId}/refresh/policy`,
        data,
      )
      .then((r) => r.data),
  listRuns: (projectId: string, modelId: string, pocketId: string) =>
    api
      .get<PocketRefreshRun[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/pockets/${pocketId}/refresh/runs`,
      )
      .then((r) => r.data),
  // F-005-08: pocket payoff metrics (hit ratio, time saved, storage, evictions).
  getMetrics: (projectId: string, modelId: string) =>
    api
      .get<PocketMetrics>(
        `/api/v1/projects/${projectId}/models/${modelId}/pockets/metrics`,
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Row Security (Phase 5.1)
// ---------------------------------------------------------------------------

export const rowSecurityApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<RowSecurityRule[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/row-security`
      )
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: RowSecurityRuleCreate) =>
    api
      .post<RowSecurityRule>(
        `/api/v1/projects/${projectId}/models/${modelId}/row-security`,
        data
      )
      .then((r) => r.data),
  update: (
    projectId: string,
    modelId: string,
    ruleId: string,
    data: RowSecurityRuleUpdate
  ) =>
    api
      .patch<RowSecurityRule>(
        `/api/v1/projects/${projectId}/models/${modelId}/row-security/${ruleId}`,
        data
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, ruleId: string) =>
    api
      .delete(
        `/api/v1/projects/${projectId}/models/${modelId}/row-security/${ruleId}`
      )
      .then((r) => r.data),
  simulate: (
    projectId: string,
    modelId: string,
    data: RowSecuritySimulateRequest
  ) =>
    api
      .post<RowSecuritySimulateResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/row-security/simulate`,
        data
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

export const logsApi = {
  queries: (
    projectId: string,
    filters: {
      modelId?: string;
      page?: number;
      pageSize?: number;
      status?: string;
      errorType?: string;
      routeType?: string;
      clientKind?: string;
      userIdentity?: string;
      dateFrom?: string;
      dateTo?: string;
      includeProbes?: boolean;
    } = {},
  ) => {
    const params = new URLSearchParams({
      page: String(filters.page ?? 1),
      page_size: String(filters.pageSize ?? 50),
    });
    if (filters.modelId) params.set("model_id", filters.modelId);
    if (filters.status) params.set("status", filters.status);
    if (filters.errorType) params.set("error_type", filters.errorType);
    if (filters.routeType) params.set("route_type", filters.routeType);
    if (filters.clientKind) params.set("client_kind", filters.clientKind);
    if (filters.userIdentity) params.set("user_identity", filters.userIdentity);
    if (filters.dateFrom) params.set("date_from", filters.dateFrom);
    if (filters.dateTo) params.set("date_to", filters.dateTo);
    if (filters.includeProbes) params.set("include_probes", "true");
    return api
      .get<PaginatedQueryLogs>(
        `/api/v1/projects/${projectId}/logs/queries?${params.toString()}`
      )
      .then((r) => r.data);
  },
  exportCsv: (
    projectId: string,
    filters: {
      modelId?: string;
      status?: string;
      errorType?: string;
      routeType?: string;
      clientKind?: string;
      userIdentity?: string;
      dateFrom?: string;
      dateTo?: string;
      includeProbes?: boolean;
    } = {},
  ) => {
    const params = new URLSearchParams();
    if (filters.modelId) params.set("model_id", filters.modelId);
    if (filters.status) params.set("status", filters.status);
    if (filters.errorType) params.set("error_type", filters.errorType);
    if (filters.routeType) params.set("route_type", filters.routeType);
    if (filters.clientKind) params.set("client_kind", filters.clientKind);
    if (filters.userIdentity) params.set("user_identity", filters.userIdentity);
    if (filters.dateFrom) params.set("date_from", filters.dateFrom);
    if (filters.dateTo) params.set("date_to", filters.dateTo);
    if (filters.includeProbes) params.set("include_probes", "true");
    const qs = params.toString();
    return api
      .get(`/api/v1/projects/${projectId}/logs/queries/export${qs ? `?${qs}` : ""}`, {
        responseType: "blob",
      })
      .then((r) => r.data as Blob);
  },
  misses: (projectId: string, modelId?: string) =>
    api
      .get<QueryMissLog[]>(
        `/api/v1/projects/${projectId}/logs/misses${modelId ? `?model_id=${modelId}` : ""}`
      )
      .then((r) => r.data),
  // F-030-25: read the stored parse/bind/route trace for one logged query.
  queryTrace: (projectId: string, queryLogId: string) =>
    api
      .get<import("./types").RouteTraceStage[]>(
        `/api/v1/projects/${projectId}/logs/queries/${queryLogId}/trace`
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Notifications
// ---------------------------------------------------------------------------

export const notificationsApi = {
  list: (projectId: string) =>
    api
      .get<NotificationRoute[]>(`/api/v1/projects/${projectId}/notifications`)
      .then((r) => r.data),
  eventTypes: (projectId: string) =>
    api
      .get<EventTypeOption[]>(
        `/api/v1/projects/${projectId}/notifications/event-types`,
      )
      .then((r) => r.data),
  create: (projectId: string, data: NotificationRouteCreate) =>
    api
      .post<NotificationRoute>(`/api/v1/projects/${projectId}/notifications`, data)
      .then((r) => r.data),
  update: (projectId: string, routeId: string, data: NotificationRouteUpdate) =>
    api
      .put<NotificationRoute>(`/api/v1/projects/${projectId}/notifications/${routeId}`, data)
      .then((r) => r.data),
  remove: (projectId: string, routeId: string) =>
    api.delete(`/api/v1/projects/${projectId}/notifications/${routeId}`),
  test: (projectId: string, data: NotificationRouteCreate) =>
    api
      .post<{ status: string }>(`/api/v1/projects/${projectId}/notifications/test`, data)
      .then((r) => r.data),
  // Bug-5999: test-send an already-saved route. The API redacts a saved
  // Slack route's webhook URL from every response, so the client cannot
  // resupply it here -- the backend decrypts the stored secret itself.
  testRoute: (projectId: string, routeId: string) =>
    api
      .post<{ status: string }>(
        `/api/v1/projects/${projectId}/notifications/${routeId}/test`,
      )
      .then((r) => r.data),
  deliveries: (projectId: string) =>
    api
      .get<NotificationDelivery[]>(
        `/api/v1/projects/${projectId}/notifications/deliveries`,
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Optimizer
// ---------------------------------------------------------------------------

export const optimizerApiClient = {
  runOptimize: (data: OptimizeRunRequest) =>
    optimizerApi.post<OptimizeRunResponse>("/api/v1/optimize/run", data).then((r) => r.data),
  sweep: () =>
    optimizerApi.post<OptimizerRunEntry>("/api/v1/optimize/sweep").then((r) => r.data),
  runModelSweep: (modelId: string) =>
    optimizerApi
      .post<OptimizerRunEntry>("/api/v1/optimize/sweep/model", { model_id: modelId })
      .then((r) => r.data),
  getRuns: () =>
    optimizerApi.get<OptimizerRunEntry[]>("/api/v1/optimize/runs").then((r) => r.data),
  getCandidates: (modelId: string) =>
    optimizerApi
      .get(`/api/v1/optimize/candidates?model_id=${modelId}`)
      .then((r) => r.data),
  retire: (aggId: string) =>
    optimizerApi.post(`/api/v1/optimize/retire/${aggId}`).then((r) => r.data),
  getSourceStatistics: (sourceId: string) =>
    optimizerApi
      .get<import("./types").SourceStatistics>(
        `/api/v1/sources/${sourceId}/statistics`,
      )
      .then((r) => r.data),
  refreshSourceStatistics: (
    sourceId: string,
    sampleLimit?: number | null,
    lowCardinalityThreshold?: number,
  ) => {
    const params = new URLSearchParams();
    if (sampleLimit !== undefined && sampleLimit !== null) {
      params.set("sample_limit", String(sampleLimit));
    }
    if (lowCardinalityThreshold !== undefined) {
      params.set("low_cardinality_threshold", String(lowCardinalityThreshold));
    }
    const qs = params.toString();
    return optimizerApi
      .post<import("./types").SourceStatistics>(
        `/api/v1/sources/${sourceId}/statistics/refresh${qs ? `?${qs}` : ""}`,
      )
      .then((r) => r.data);
  },
  updateTableStatsCadence: (
    sourceId: string,
    modelTableId: string,
    cadence: import("./types").StatsRefreshCadence,
  ) =>
    optimizerApi
      .patch<import("./types").TableStatistics>(
        `/api/v1/sources/${sourceId}/statistics/tables/${modelTableId}/cadence`,
        { cadence },
      )
      .then((r) => r.data),
  getPredictivePreview: (modelId: string, topK = 20) =>
    optimizerApi
      .get<import("./types").PredictivePreview>(
        `/api/v1/models/${modelId}/predictive/preview?top_k=${topK}`,
      )
      .then((r) => r.data),
  runPredictiveBuild: (
    modelId: string,
    selections?: Array<{ grain: string[]; measure_names: string[] }>,
  ) =>
    optimizerApi
      .post<import("./types").PredictiveBuildAccepted>(
        `/api/v1/models/${modelId}/predictive/build`,
        selections ? { selections } : {},
      )
      .then((r) => r.data),
  getPredictiveBuildStatus: (modelId: string, buildId: string) =>
    optimizerApi
      .get<import("./types").PredictiveBuildResult>(
        `/api/v1/models/${modelId}/predictive/build/${buildId}`,
      )
      .then((r) => r.data),
  getAggregateLifecycle: (
    modelId: string,
    params: {
      eventType?: string;
      aggregateId?: string;
      limit?: number;
    } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.eventType) search.set("event_type", params.eventType);
    if (params.aggregateId) search.set("aggregate_id", params.aggregateId);
    search.set("limit", String(params.limit ?? 200));
    return optimizerApi
      .get<import("./types").AggregateLifecycleResponse>(
        `/api/v1/models/${modelId}/aggregates/lifecycle?${search.toString()}`,
      )
      .then((r) => r.data);
  },
  getColdStartLatency: (
    modelId: string,
    params: { sampleSize?: number; baselineDays?: number } = {},
  ) => {
    const search = new URLSearchParams();
    search.set("sample_size", String(params.sampleSize ?? 20));
    search.set("baseline_days", String(params.baselineDays ?? 14));
    return optimizerApi
      .get<import("./types").ColdStartResponse>(
        `/api/v1/models/${modelId}/cold-start?${search.toString()}`,
      )
      .then((r) => r.data);
  },
  getPocketSuggestions: (modelId: string, days = 30) =>
    optimizerApi
      .get<PocketSuggestionsResponse>(
        `/api/v1/optimize/pocket-suggestions?model_id=${modelId}&days=${days}`,
      )
      .then((r) => r.data),
  getModelROI: (modelId: string) =>
    optimizerApi
      .get<import("./types").AggregateROI[]>(`/api/v1/optimize/models/${modelId}/aggregates/roi`)
      .then((r) => r.data),
  getTenantROISummary: () =>
    optimizerApi
      .get<import("./types").ROISummaryItem[]>("/api/v1/optimize/admin/aggregates/roi-summary")
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Scheduler
// ---------------------------------------------------------------------------

export const schedulerApiClient = {
  triggerRefresh: (data: SchedulerTriggerRequest) =>
    schedulerApi
      .post<TriggerRefreshResponse>("/api/v1/scheduler/trigger/refresh", data)
      .then((r) => r.data),
  triggerDrift: (data: SchedulerTriggerRequest) =>
    schedulerApi
      .post("/api/v1/scheduler/trigger/schema-drift", data)
      .then((r) => r.data),
  triggerRetirement: (data: SchedulerTriggerRequest) =>
    schedulerApi
      .post("/api/v1/scheduler/trigger/retirement", data)
      .then((r) => r.data),
  jobs: () =>
    schedulerApi.get("/api/v1/scheduler/jobs").then((r) => r.data),
  reseedDemo: () =>
    schedulerApi
      .post("/api/v1/scheduler/trigger/demo-reseed")
      .then((r) => r.data),
  reseedDemoStatus: () =>
    schedulerApi
      .get<{ job: string | null; state: string; returncode: number | null; output: string }>(
        "/api/v1/scheduler/trigger/demo-reseed/status",
      )
      .then((r) => r.data),
  getSLA: (projectId: string, modelId: string) =>
    schedulerApi
      .get<SLAConfig>(`/api/v1/projects/${projectId}/models/${modelId}/sla`)
      .then((r) => r.data),
  createSLA: (projectId: string, modelId: string, data: SLAConfigCreate) =>
    schedulerApi
      .post<SLAConfig>(`/api/v1/projects/${projectId}/models/${modelId}/sla`, data)
      .then((r) => r.data),
  updateSLA: (projectId: string, modelId: string, data: Partial<SLAConfigCreate>) =>
    schedulerApi
      .patch<SLAConfig>(`/api/v1/projects/${projectId}/models/${modelId}/sla`, data)
      .then((r) => r.data),
  deleteSLA: (projectId: string, modelId: string) =>
    schedulerApi
      .delete(`/api/v1/projects/${projectId}/models/${modelId}/sla`)
      .then((r) => r.data),

  getDependencies: (modelId: string) =>
    schedulerApi
      .get<{
        dependencies: Array<{
          id: string;
          upstream_aggregate_id: string;
          downstream_aggregate_id: string;
          upstream_name: string | null;
          downstream_name: string | null;
        }>;
        execution_order: string[];
      }>(`/api/v1/scheduler/dependencies/${modelId}`)
      .then((r) => r.data),
  createDependency: (upstream: string, downstream: string) =>
    schedulerApi
      .post(`/api/v1/scheduler/dependencies`, {
        upstream_aggregate_id: upstream,
        downstream_aggregate_id: downstream,
      })
      .then((r) => r.data),
  deleteDependency: (depId: string) =>
    schedulerApi
      .delete(`/api/v1/scheduler/dependencies/${depId}`)
      .then((r) => r.data),

  triggerKpiSnapshotSweep: () =>
    schedulerApi
      .post<{
        snapshots_written: number;
        kpis_failed: number;
        models_attempted: number;
        models_failed: number;
        models_skipped: number;
        // Bug-7139: kpi_latest upsert outcome counters.
        latest_persisted: number;
        latest_failed: number;
        // Bug-7982 completion round: a write the epoch-monotonicity guard
        // suppressed (a fresher row was already published) — not a
        // failure, excluded from `status`, but must stay visible so a
        // chronic suppression is never indistinguishable from a healthy
        // "nothing to do" sweep.
        latest_suppressed: number;
        status: string;
        error_message: string | null;
      }>("/api/v1/scheduler/trigger/kpi-snapshot-sweep")
      .then((r) => r.data),

  triggerKpiSnapshotPurge: () =>
    schedulerApi
      .post<{ purged: number }>("/api/v1/scheduler/trigger/kpi-snapshot-purge")
      .then((r) => r.data),

  triggerPocketSweep: () =>
    schedulerApi
      .post<{ refreshed: number }>("/api/v1/scheduler/trigger/pocket-sweep")
      .then((r) => r.data),

  triggerPocketEviction: () =>
    schedulerApi
      .post<{ evicted: number }>("/api/v1/scheduler/trigger/pocket-eviction")
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Glossary v1 (semantic-layer Phase 3)
// ---------------------------------------------------------------------------

export const glossaryApi = {
  list: (projectId: string, modelId: string, statusFilter?: string) =>
    api
      .get<GlossaryEntry[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/glossary` +
          (statusFilter ? `?status_filter=${encodeURIComponent(statusFilter)}` : ""),
      )
      .then((r) => r.data),
  bootstrap: (projectId: string, modelId: string) =>
    api
      .post<GlossaryBootstrapResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/glossary/bootstrap`,
      )
      .then((r) => r.data),
  bootstrapJobStatus: (projectId: string, modelId: string, jobId: string) =>
    api
      .get<GlossaryBootstrapResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/glossary/bootstrap/jobs/${jobId}`,
      )
      .then((r) => r.data),
  approve: (projectId: string, modelId: string, entryId: string) =>
    api
      .post<GlossaryEntry>(
        `/api/v1/projects/${projectId}/models/${modelId}/glossary/${entryId}/approve`,
      )
      .then((r) => r.data),
  approveBulk: (projectId: string, modelId: string) =>
    api
      .post<{ approved_count: number }>(
        `/api/v1/projects/${projectId}/models/${modelId}/glossary/approve-bulk`,
      )
      .then((r) => r.data),
  update: (
    projectId: string,
    modelId: string,
    entryId: string,
    data: GlossaryEntryUpdate,
  ) =>
    api
      .patch<GlossaryEntry>(
        `/api/v1/projects/${projectId}/models/${modelId}/glossary/${entryId}`,
        data,
      )
      .then((r) => r.data),
  reject: (projectId: string, modelId: string, entryId: string) =>
    api.post(
      `/api/v1/projects/${projectId}/models/${modelId}/glossary/${entryId}/reject`,
    ),
  delete: (projectId: string, modelId: string, entryId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/glossary/${entryId}`,
    ),
  deleteBulk: (
    projectId: string,
    modelId: string,
    scope: "all" | "heuristic" | "non_manual",
  ) =>
    api
      .post<{ deleted_count: number }>(
        `/api/v1/projects/${projectId}/models/${modelId}/glossary/delete-bulk`,
        { scope },
      )
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: GlossaryEntryCreate) =>
    api
      .post<GlossaryEntry>(
        `/api/v1/projects/${projectId}/models/${modelId}/glossary`,
        data,
      )
      .then((r) => r.data),
  share: (projectId: string, modelId: string) =>
    api
      .post<{ token: string; frontend_path: string }>(
        `/api/v1/projects/${projectId}/models/${modelId}/glossary/share`,
      )
      .then((r) => r.data),
  revokeShareTokens: (projectId: string, modelId: string) =>
    api
      .post<{ revoked_count: number }>(
        `/api/v1/projects/${projectId}/models/${modelId}/glossary/share/revoke`,
      )
      .then((r) => r.data),
  regenerateShareToken: (projectId: string, modelId: string) =>
    api
      .post<{ token: string; frontend_path: string }>(
        `/api/v1/projects/${projectId}/models/${modelId}/glossary/share/regenerate`,
      )
      .then((r) => r.data),
  importCsv: (projectId: string, modelId: string, csv: string) =>
    api
      .post<{ created: number; errors: { line: number; error: string }[] }>(
        `/api/v1/projects/${projectId}/models/${modelId}/glossary/import`,
        { csv },
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Per-model alias map (phrase -> canonical attribute)
// ---------------------------------------------------------------------------

export type AliasMap = Record<string, string>;

export const aliasMapApi = {
  get: (projectId: string, modelId: string) =>
    api
      .get<{ model_id: string; alias_map: AliasMap }>(
        `/api/v1/projects/${projectId}/models/${modelId}/alias-map`,
      )
      .then((r) => r.data.alias_map ?? {}),
  replace: (projectId: string, modelId: string, aliasMap: AliasMap) =>
    api
      .put<{ model_id: string; alias_map: AliasMap }>(
        `/api/v1/projects/${projectId}/models/${modelId}/alias-map`,
        { alias_map: aliasMap },
      )
      .then((r) => r.data.alias_map ?? {}),
  importJson: (
    projectId: string,
    modelId: string,
    aliasMap: AliasMap,
    mode: "replace" | "merge" = "merge",
  ) =>
    api
      .post<{ model_id: string; alias_map: AliasMap }>(
        `/api/v1/projects/${projectId}/models/${modelId}/alias-map/import`,
        { alias_map: aliasMap, mode },
      )
      .then((r) => r.data.alias_map ?? {}),
};

// ---------------------------------------------------------------------------
// Query Router — validate / explain / execute
// ---------------------------------------------------------------------------

function _withPersona(
  data: QueryRouterRequest,
  personaId?: string | null,
): QueryRouterRequest {
  if (!personaId) return data;
  return { ...data, persona_id: personaId };
}

export const queryRouterApiClient = {
  validate: (data: QueryRouterRequest, personaId?: string | null) =>
    queryRouterApi
      .post<QueryValidateResponse>(
        "/api/v1/validate",
        _withPersona(data, personaId),
      )
      .then((r) => r.data),
  explain: (data: QueryRouterRequest, personaId?: string | null) =>
    queryRouterApi
      .post<QueryExplainResponse>(
        "/api/v1/explain",
        _withPersona(data, personaId),
      )
      .then((r) => r.data),
  execute: (data: QueryRouterRequest, personaId?: string | null, signal?: AbortSignal) =>
    queryRouterApi
      .post<QueryExecuteResponse>(
        "/api/v1/execute",
        _withPersona(data, personaId),
        signal ? { signal } : undefined,
      )
      .then((r) => r.data),
  discoverMembers: (modelId: string, dimensionName: string) =>
    queryRouterApi
      .post<{
        members: Array<{ name: string; key: string }>;
        levels: string[];
        // Bug-8453 / R4 finding 3: the denial channel on the member-discovery
        // surface. Consumers classify it with utils/rowSecurity.ts.
        security_rules_applied?: string[];
      }>(
        "/api/v1/discover/members",
        { model_id: modelId, dimension_name: dimensionName },
      )
      .then((r) => r.data),
  drillThrough: (
    measureId: string,
    data: DrillThroughRequest,
    personaId?: string | null,
  ) =>
    queryRouterApi
      .post<DrillThroughResponse>(
        `/api/v1/measures/${measureId}/drill-through`,
        personaId ? { ...data, persona_id: personaId } : data,
      )
      .then((r) => r.data),
  drillOptions: (
    measureId: string,
    data: DrillOptionsRequest,
    personaId?: string | null,
  ) =>
    queryRouterApi
      .post<DrillOptionsResponse>(
        `/api/v1/measures/${measureId}/drill-options`,
        // F-019-18: forward the selected persona so the hierarchy picker is
        // filtered by the same effective persona /drill-through honours —
        // otherwise the picker offers a path the drill then blocks.
        personaId ? { ...data, persona_id: personaId } : data,
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// AI Scheduler Config (per-model)
// ---------------------------------------------------------------------------

export const aiSchedulerApi = {
  get: (projectId: string, modelId: string) =>
    api
      .get<AISchedulerConfig>(
        `/api/v1/projects/${projectId}/models/${modelId}/scheduler-config`
      )
      .then((r) => r.data),
  update: (projectId: string, modelId: string, data: ModelAISchedulerConfigUpdate) =>
    api
      .put<AISchedulerConfig>(
        `/api/v1/projects/${projectId}/models/${modelId}/scheduler-config`,
        data
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// AI Optimizer
// ---------------------------------------------------------------------------

export const aiOptimizerApi = {
  triggerRun: (data: AIOptimizerTriggerRequest) =>
    optimizerApi
      .post<AIOptimizerRunStartResponse>("/api/v1/optimize/ai/run", data)
      .then((r) => r.data),
  // Bug-6558 — removed dead `tenant_id` query param.  The optimizer
  // route resolves the tenant from the JWT; it never declared tenant_id
  // as a query parameter.
  listRuns: (modelId?: string) => {
    const params = modelId ? `?model_id=${modelId}` : "";
    return optimizerApi
      .get<AIOptimizerRun[]>(`/api/v1/optimize/ai/runs${params}`)
      .then((r) => r.data);
  },
  getRun: (runId: string) =>
    optimizerApi
      .get<AIOptimizerRun>(`/api/v1/optimize/ai/runs/${runId}`)
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// LLM Provider Configs
// ---------------------------------------------------------------------------

// Post 2026-04 admin-config restructure: LLMProviderConfig is project-scoped.
// All paths now require a projectId. The setActive concept is dropped — the
// project's default agent / judge LLM is selected via Agent settings pickers
// (agent.llm_config_id / agent.judge_llm_config_id project settings).

export const llmConfigsApi = {
  list: (projectId: string) =>
    api.get<LLMProviderConfig[]>(
      `/api/v1/projects/${projectId}/llm-configs`,
    ).then((r) => r.data),
  create: (projectId: string, data: LLMProviderConfigCreate) =>
    api.post<LLMProviderConfig>(
      `/api/v1/projects/${projectId}/llm-configs`,
      data,
    ).then((r) => r.data),
  update: (projectId: string, configId: string, data: LLMProviderConfigUpdate) =>
    api.put<LLMProviderConfig>(
      `/api/v1/projects/${projectId}/llm-configs/${configId}`,
      data,
    ).then((r) => r.data),
  delete: (projectId: string, configId: string) =>
    api.delete(`/api/v1/projects/${projectId}/llm-configs/${configId}`),
  test: (projectId: string, configId: string) =>
    api.post<LLMConnectionTestResponse>(
      `/api/v1/projects/${projectId}/llm-configs/${configId}/test`,
    ).then((r) => r.data),
  testAdhoc: (projectId: string, data: LLMConnectionTestRequest) =>
    api.post<LLMConnectionTestResponse>(
      `/api/v1/projects/${projectId}/llm-configs/test-adhoc`,
      data,
    ).then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Personas (Phase 8.B)
// ---------------------------------------------------------------------------

export const personasApi = {
  list: (projectId: string, modelId: string, opts?: { forAudience?: boolean }) => {
    const qs = opts?.forAudience ? "?for_audience=true" : "";
    return api
      .get<Persona[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/personas${qs}`,
      )
      .then((r) => r.data);
  },
  get: (projectId: string, modelId: string, personaId: string) =>
    api
      .get<Persona>(
        `/api/v1/projects/${projectId}/models/${modelId}/personas/${personaId}`,
      )
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: PersonaCreate) =>
    api
      .post<Persona>(
        `/api/v1/projects/${projectId}/models/${modelId}/personas`,
        data,
      )
      .then((r) => r.data),
  update: (
    projectId: string,
    modelId: string,
    personaId: string,
    data: PersonaUpdate,
  ) =>
    api
      .patch<Persona>(
        `/api/v1/projects/${projectId}/models/${modelId}/personas/${personaId}`,
        data,
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, personaId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/personas/${personaId}`,
    ),
  resolve: (
    projectId: string,
    modelId: string,
    personaId: string,
    measureId: string,
  ) =>
    api
      .get<PersonaResolution>(
        `/api/v1/projects/${projectId}/models/${modelId}/personas/${personaId}/resolution?measure_id=${measureId}`,
      )
      .then((r) => r.data),
};


export interface PocketSuggestionItem {
  predicate_set_hash: string;
  predicates: { column_name: string; operator: string; value: unknown }[];
  hit_count: number;
  frequency: number;
  estimated_size_bytes: number;
  score: number;
  sample_measures: string[];
  sample_dimensions: string[];
  status: "suggested" | "hinted";
  budget_detail: string;
  // F-005-13: a model-subset SQL the create drawer is prefilled with.
  defining_sql: string;
}

export interface PocketMetrics {
  total_pockets: number;
  fresh_pockets: number;
  stale_pockets: number;
  invalidating_pockets: number;
  failed_pockets: number;
  retired_pockets: number;
  pocket_hit_rate: number;
  pocket_time_saved_ms: number;
  pocket_storage_bytes: number;
  pocket_evictions_24h: number;
  top_pockets: {
    pocket_id: string;
    physical_table_name: string;
    status: string;
    hit_count: number;
    ttl_days: number;
    // F-005-22: false when the pocket is fresh but has not matched a query
    // since its last refresh.
    matched_since_refresh?: boolean;
  }[];
  // F-005-22: count of fresh pockets with zero matches since their last
  // refresh, plus the most common skip reason the router logged for this model.
  zero_match_fresh_pockets?: number;
  top_skip_reason?: string | null;
  top_skip_count?: number;
}

export interface PocketSuggestionsResponse {
  suggestions: PocketSuggestionItem[];
  budget_bytes: number | null;
  used_bytes: number;
  remaining_bytes: number | null;
}

export interface QueryVolumeBucket {
  bucket: string;
  count: number;
}
export interface TopMeasureEntry {
  measure_name: string;
  query_count: number;
}
export interface TopAggregateEntry {
  aggregate_id: string;
  physical_table_name: string;
  query_count: number;
  grain: string[] | null;
}
export interface AnalyticsSummaryData {
  total_queries: number;
  // F-030-08: headline combined acceleration rate (aggregate + pocket routes).
  acceleration_rate: number;
  aggregate_hit_rate: number;
  top_measure: string | null;
  avg_response_ms: number | null;
}

export interface RoutingBreakdownEntry {
  route_type: string;
  count: number;
  pct: number;
}

export interface EstimatedSavingsData {
  accelerated_queries: number;
  total_queries: number;
  time_saved_ms: number;
  avg_source_ms: number | null;
  avg_accelerated_ms: number | null;
}

export interface TopUserEntry {
  user_identity: string;
  query_count: number;
}

export const analyticsApi = {
  queryVolume: (projectId: string, modelId: string, days = 30) =>
    api
      .get<QueryVolumeBucket[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/analytics/query-volume?days=${days}`,
      )
      .then((r) => r.data),
  topMeasures: (projectId: string, modelId: string, days = 30, limit = 10) =>
    api
      .get<TopMeasureEntry[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/analytics/top-measures?days=${days}&limit=${limit}`,
      )
      .then((r) => r.data),
  topAggregates: (projectId: string, modelId: string, days = 30, limit = 10) =>
    api
      .get<TopAggregateEntry[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/analytics/top-aggregates?days=${days}&limit=${limit}`,
      )
      .then((r) => r.data),
  summary: (projectId: string, modelId: string, days = 7) =>
    api
      .get<AnalyticsSummaryData>(
        `/api/v1/projects/${projectId}/models/${modelId}/analytics/summary?days=${days}`,
      )
      .then((r) => r.data),
  routingBreakdown: (projectId: string, modelId: string, days = 30) =>
    api
      .get<RoutingBreakdownEntry[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/analytics/routing-breakdown?days=${days}`,
      )
      .then((r) => r.data),
  estimatedSavings: (projectId: string, modelId: string, days = 30) =>
    api
      .get<EstimatedSavingsData>(
        `/api/v1/projects/${projectId}/models/${modelId}/analytics/estimated-savings?days=${days}`,
      )
      .then((r) => r.data),
  topUsers: (projectId: string, modelId: string, days = 30, limit = 10) =>
    api
      .get<TopUserEntry[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/analytics/top-users?days=${days}&limit=${limit}`,
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Audit events
// ---------------------------------------------------------------------------

import type {
  AuditEventListResponse,
  AuthBackendsResponse,
  GroupMapping,
  GroupMappingCreate,
  ModelParameter,
  ModelParameterCreate,
  ModelParameterUpdate,
  JoinPopulationHealthResponse,
  RelationshipHealthResponse,
  SchemaChangeEvent,
  SchemaChangeEventListResponse,
  SecurityAuditListResponse,
  WebhookCreate,
  WebhookCreateResponse,
  WebhookDelivery,
  WebhookEndpoint,
  WebhookUpdate,
  WebhookUpdateResponse,
} from "./types";

export interface AuditEventFilters {
  actor_email?: string;
  action?: string;
  target_type?: string;
  severity?: string;
  from_date?: string;
  to_date?: string;
  limit?: number;
  offset?: number;
}

export const auditApi = {
  list: (filters: AuditEventFilters = {}) => {
    const params = new URLSearchParams();
    if (filters.actor_email) params.set("actor_email", filters.actor_email);
    if (filters.action) params.set("action", filters.action);
    if (filters.target_type) params.set("target_type", filters.target_type);
    if (filters.severity) params.set("severity", filters.severity);
    if (filters.from_date) params.set("from_date", filters.from_date);
    if (filters.to_date) params.set("to_date", filters.to_date);
    params.set("limit", String(filters.limit ?? 50));
    params.set("offset", String(filters.offset ?? 0));
    return api
      .get<AuditEventListResponse>(`/api/v1/admin/audit-events?${params}`)
      .then((r) => r.data);
  },
  listActions: () =>
    api.get<string[]>("/api/v1/admin/audit-events/actions").then((r) => r.data),
  exportCsv: (filters: AuditEventFilters = {}) => {
    const params = new URLSearchParams();
    if (filters.actor_email) params.set("actor_email", filters.actor_email);
    if (filters.action) params.set("action", filters.action);
    if (filters.target_type) params.set("target_type", filters.target_type);
    if (filters.severity) params.set("severity", filters.severity);
    if (filters.from_date) params.set("from_date", filters.from_date);
    if (filters.to_date) params.set("to_date", filters.to_date);
    return api
      .get(`/api/v1/admin/audit-events/export?${params}`, {
        responseType: "blob",
      })
      .then((r) => r.data as Blob);
  },
};

export const ssoApi = {
  getBackends: (tenantId?: string) =>
    api
      .get<AuthBackendsResponse>("/api/v1/auth/backends", {
        params: tenantId ? { tenant_id: tenantId } : undefined,
      })
      .then((r) => r.data),
  samlLoginUrl: (tenantId: string) =>
    `/api/v1/auth/saml/login?tenant_id=${encodeURIComponent(tenantId)}`,
  oidcLoginUrl: (tenantId: string) =>
    `/api/v1/auth/oidc/login?tenant_id=${encodeURIComponent(tenantId)}`,
  getConfig: () =>
    api.get<SsoConfig>("/api/v1/auth/sso-config").then((r) => r.data),
  putConfig: (data: SsoConfigWrite) =>
    api.put<SsoConfig>("/api/v1/auth/sso-config", data).then((r) => r.data),
};

export type SsoConfig = {
  saml: Record<string, unknown>;
  oidc: Record<string, unknown> & { client_secret_set?: boolean };
};

export type SsoConfigWrite = {
  saml?: Record<string, unknown>;
  oidc?: Record<string, unknown>;
};

export type EmbedTokenRow = {
  jti: string;
  actor_email?: string | null;
  user_identity: string;
  capabilities: string[];
  expires_at?: string | null;
  revoked_at?: string | null;
  created_at?: string | null;
};

export const embedTokensApi = {
  list: () =>
    api.get<EmbedTokenRow[]>("/api/v1/auth/embed-tokens").then((r) => r.data),
  revoke: (jti: string) =>
    api.delete(`/api/v1/auth/embed-token/${encodeURIComponent(jti)}`),
};

export const groupMappingsApi = {
  list: () =>
    api.get<GroupMapping[]>("/api/v1/admin/group-mappings").then((r) => r.data),
  create: (data: GroupMappingCreate) =>
    api.post<GroupMapping>("/api/v1/admin/group-mappings", data).then((r) => r.data),
  update: (id: string, data: { role: string }) =>
    api.put<GroupMapping>(`/api/v1/admin/group-mappings/${id}`, data).then((r) => r.data),
  delete: (id: string) =>
    api.delete(`/api/v1/admin/group-mappings/${id}`),
};

// ---------------------------------------------------------------------------
// Webhooks
// ---------------------------------------------------------------------------

export const webhooksApi = {
  list: () =>
    api.get<WebhookEndpoint[]>("/api/v1/admin/webhooks").then((r) => r.data),
  eventTypes: () =>
    api
      .get<EventTypeOption[]>("/api/v1/admin/webhooks/event-types")
      .then((r) => r.data),
  create: (data: WebhookCreate) =>
    api.post<WebhookCreateResponse>("/api/v1/admin/webhooks", data).then((r) => r.data),
  update: (id: string, data: WebhookUpdate) =>
    api.put<WebhookUpdateResponse>(`/api/v1/admin/webhooks/${id}`, data).then((r) => r.data),
  delete: (id: string) =>
    api.delete(`/api/v1/admin/webhooks/${id}`),
  test: (id: string) =>
    api.post<WebhookDelivery>(`/api/v1/admin/webhooks/${id}/test`).then((r) => r.data),
  rotateSecret: (id: string) =>
    api.post<{ signing_secret: string }>(`/api/v1/admin/webhooks/${id}/rotate-secret`).then((r) => r.data),
  deliveries: (id: string, limit = 50, offset = 0) =>
    api.get<WebhookDelivery[]>(`/api/v1/admin/webhooks/${id}/deliveries?limit=${limit}&offset=${offset}`).then((r) => r.data),
  dlqCount: () =>
    api.get<{ count: number }>(`/api/v1/admin/webhooks/dlq/count`).then((r) => r.data),
  dlq: (limit = 50, offset = 0) =>
    api.get<WebhookDelivery[]>(`/api/v1/admin/webhooks/dlq?limit=${limit}&offset=${offset}`).then((r) => r.data),
  retryDlq: (deliveryId: string) =>
    api.post<WebhookDelivery>(`/api/v1/admin/webhooks/dlq/${deliveryId}/retry`).then((r) => r.data),
  deleteDlq: (deliveryId: string) =>
    api.delete(`/api/v1/admin/webhooks/dlq/${deliveryId}`),
};

// ---------------------------------------------------------------------------
// Model Parameters
// ---------------------------------------------------------------------------

export const parametersApi = {
  list: (projectId: string, modelId: string) =>
    api.get<ModelParameter[]>(`/api/v1/projects/${projectId}/models/${modelId}/parameters`).then((r) => r.data),
  create: (projectId: string, modelId: string, data: ModelParameterCreate) =>
    api.post<ModelParameter>(`/api/v1/projects/${projectId}/models/${modelId}/parameters`, data).then((r) => r.data),
  update: (projectId: string, modelId: string, paramId: string, data: ModelParameterUpdate) =>
    api.put<ModelParameter>(`/api/v1/projects/${projectId}/models/${modelId}/parameters/${paramId}`, data).then((r) => r.data),
  delete: (projectId: string, modelId: string, paramId: string) =>
    api.delete(`/api/v1/projects/${projectId}/models/${modelId}/parameters/${paramId}`),
};

// ---------------------------------------------------------------------------
// Schema Drift
// ---------------------------------------------------------------------------

export const schemaDriftApi = {
  list: (modelId?: string, includeAcknowledged = false) => {
    const params = new URLSearchParams();
    if (modelId) params.set("model_id", modelId);
    if (includeAcknowledged) params.set("include_acknowledged", "true");
    return api
      .get<SchemaChangeEventListResponse>(`/api/v1/admin/schema-drift?${params}`)
      .then((r) => r.data);
  },
  acknowledge: (eventId: string) =>
    api
      .patch<SchemaChangeEvent>(`/api/v1/admin/schema-drift/${eventId}/acknowledge`)
      .then((r) => r.data),
};

export const relationshipHealthApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<RelationshipHealthResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/relationship-health`,
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Join population governance health (Model Health surface)
// ---------------------------------------------------------------------------

export const joinPopulationHealthApi = {
  get: (projectId: string, modelId: string) =>
    api
      .get<JoinPopulationHealthResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/join-population-health`,
      )
      .then((r) => r.data),
};

export const schemaChangesApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<SchemaChangeEvent[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/schema-changes`,
      )
      .then((r) => r.data),
  acknowledge: (projectId: string, modelId: string, eventId: string) =>
    api
      .post(
        `/api/v1/projects/${projectId}/models/${modelId}/schema-changes/${eventId}/acknowledge`,
      )
      .then((r) => r.data),
};

export const securityAuditApi = {
  list: (params?: { model_id?: string; from?: string; to?: string; limit?: number; offset?: number }) => {
    const p = new URLSearchParams();
    if (params?.model_id) p.set("model_id", params.model_id);
    if (params?.from) p.set("from", params.from);
    if (params?.to) p.set("to", params.to);
    if (params?.limit !== undefined) p.set("limit", String(params.limit));
    if (params?.offset !== undefined) p.set("offset", String(params.offset));
    return api
      .get<SecurityAuditListResponse>(`/api/v1/admin/security-audit?${p}`)
      .then((r) => r.data);
  },
};

// ---------------------------------------------------------------------------
// Usage & Downstream Assets (routes keep /impact + /downstream-assets prefixes)
// ---------------------------------------------------------------------------

export const downstreamAssetsApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<DownstreamAsset[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/downstream-assets`
      )
      .then((r) => r.data),
  summary: (projectId: string, modelId: string) =>
    api
      .get<DownstreamAssetSummary>(
        `/api/v1/projects/${projectId}/models/${modelId}/downstream-assets/summary`
      )
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: DownstreamAssetCreate) =>
    api
      .post<DownstreamAsset>(
        `/api/v1/projects/${projectId}/models/${modelId}/downstream-assets`,
        data
      )
      .then((r) => r.data),
  update: (
    projectId: string,
    modelId: string,
    assetId: string,
    data: DownstreamAssetUpdate
  ) =>
    api
      .put<DownstreamAsset>(
        `/api/v1/projects/${projectId}/models/${modelId}/downstream-assets/${assetId}`,
        data
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, assetId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/downstream-assets/${assetId}`
    ),
};

export const dataTagsApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<DataTag[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/data-tags`
      )
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: DataTagCreate) =>
    api
      .post<DataTag>(
        `/api/v1/projects/${projectId}/models/${modelId}/data-tags`,
        data
      )
      .then((r) => r.data),
  update: (
    projectId: string,
    modelId: string,
    tagId: string,
    data: DataTagUpdate
  ) =>
    api
      .put<DataTag>(
        `/api/v1/projects/${projectId}/models/${modelId}/data-tags/${tagId}`,
        data
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, tagId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/data-tags/${tagId}`
    ),
  getPersonaRestrictions: (
    projectId: string,
    modelId: string,
    personaId: string
  ) =>
    api
      .get<PersonaTagRestriction[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/personas/${personaId}/tag-restrictions`
      )
      .then((r) => r.data),
  setPersonaRestrictions: (
    projectId: string,
    modelId: string,
    personaId: string,
    data: PersonaTagRestrictionRequest
  ) =>
    api
      .put<PersonaTagRestriction[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/personas/${personaId}/tag-restrictions`,
        data
      )
      .then((r) => r.data),
};

export const namedSetsApi = {
  /** Bug-7949: effective member cap from the backend, updated on every list fetch. */
  _cachedMemberCap: 1000 as number,
  list: (projectId: string, modelId: string) =>
    api
      .get<import("./types").NamedSet[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-sets`,
      )
      .then((r) => {
        // Bug-7949: extract the effective member cap from the response header.
        const capHeader = r.headers?.["x-named-list-member-cap"];
        if (capHeader) {
          namedSetsApi._cachedMemberCap = parseInt(capHeader, 10) || 1000;
        }
        return r.data;
      }),
  create: (projectId: string, modelId: string, data: import("./types").NamedSetCreate) =>
    api
      .post<import("./types").NamedSet>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-sets`,
        data,
      )
      .then((r) => r.data),
  update: (projectId: string, modelId: string, id: string, data: import("./types").NamedSetUpdate) =>
    api
      .patch<import("./types").NamedSet>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-sets/${id}`,
        data,
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, id: string) =>
    api.delete(`/api/v1/projects/${projectId}/models/${modelId}/named-sets/${id}`),
  validate: (projectId: string, modelId: string, data: import("./types").NamedSetValidateRequest) =>
    api
      .post<import("./types").NamedSetValidateResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-sets/validate`,
        data,
      )
      .then((r) => r.data),
  preview: (projectId: string, modelId: string, id: string) =>
    api
      .post<import("./types").NamedSetPreviewResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-sets/${id}/preview`,
      )
      .then((r) => r.data),
  previewByDefinition: (projectId: string, modelId: string, data: import("./types").NamedSetValidateRequest) =>
    api
      .post<import("./types").NamedSetPreviewResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-sets/preview-by-definition`,
        data,
      )
      .then((r) => r.data),
  versions: (projectId: string, modelId: string, id: string) =>
    api
      .get<import("./types").VersionEntry[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-sets/${id}/versions`,
      )
      .then((r) => r.data),
  revert: (projectId: string, modelId: string, id: string, versionNumber: number) =>
    api
      .post<import("./types").NamedSet>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-sets/${id}/versions/${versionNumber}/revert`,
      )
      .then((r) => r.data),
  certify: (projectId: string, modelId: string, id: string) =>
    api
      .post<import("./types").NamedSet>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-sets/${id}/certify`,
        {},
      )
      .then((r) => r.data),
  deprecate: (projectId: string, modelId: string, id: string, data: import("./types").DeprecateRequest) =>
    api
      .post<import("./types").NamedSet>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-sets/${id}/deprecate`,
        data,
      )
      .then((r) => r.data),
  reportUsage: (projectId: string, modelId: string, id: string, data: import("./types").EntityUsageCreate) =>
    api
      .post(
        `/api/v1/projects/${projectId}/models/${modelId}/named-sets/${id}/usage`,
        data,
      )
      .then((r) => r.data),
  listUsage: (projectId: string, modelId: string, id: string) =>
    api
      .get<import("./types").EntityUsageEntry[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-sets/${id}/usage`,
      )
      .then((r) => r.data),
  refresh: (projectId: string, modelId: string, id: string) =>
    api
      .post<import("./types").NamedSet>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-sets/${id}/refresh`,
      )
      .then((r) => r.data),
};

export const namedQueriesApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<import("./types").NamedQuery[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-queries`,
      )
      .then((r) => r.data),
  get: (projectId: string, modelId: string, id: string) =>
    api
      .get<import("./types").NamedQuery>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-queries/${id}`,
      )
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: import("./types").NamedQueryCreate) =>
    api
      .post<import("./types").NamedQuery>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-queries`,
        data,
      )
      .then((r) => r.data),
  update: (projectId: string, modelId: string, id: string, data: import("./types").NamedQueryUpdate) =>
    api
      .patch<import("./types").NamedQuery>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-queries/${id}`,
        data,
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, id: string) =>
    api.delete(`/api/v1/projects/${projectId}/models/${modelId}/named-queries/${id}`),
  validate: (projectId: string, modelId: string, data: import("./types").NamedQueryValidateRequest) =>
    api
      .post<import("./types").NamedQueryValidateResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-queries/validate`,
        data,
      )
      .then((r) => r.data),
  refresh: (projectId: string, modelId: string, id: string) =>
    api
      .post<import("./types").NamedQueryRefreshRun>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-queries/${id}/refresh`,
      )
      .then((r) => r.data),
  listRuns: (projectId: string, modelId: string, id: string) =>
    api
      .get<import("./types").NamedQueryRefreshRun[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-queries/${id}/refresh/runs`,
      )
      .then((r) => r.data),
  // F-026-01 / F-101-07: the refresh schedule is its own snapshot-owned
  // resource. PATCH on the Named Query ignores policy fields, so an edit to the
  // cron must go through GET/PUT /refresh/policy. Create sends the schedule
  // inline (backend inserts the policy row when refresh_policy == "schedule").
  getPolicy: (projectId: string, modelId: string, id: string) =>
    api
      .get<import("./types").NamedQueryRefreshPolicy>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-queries/${id}/refresh/policy`,
      )
      .then((r) => r.data),
  putPolicy: (
    projectId: string,
    modelId: string,
    id: string,
    data: import("./types").NamedQueryRefreshPolicyUpsert,
  ) =>
    api
      .put<import("./types").NamedQueryRefreshPolicy>(
        `/api/v1/projects/${projectId}/models/${modelId}/named-queries/${id}/refresh/policy`,
        data,
      )
      .then((r) => r.data),
};

export const kpisApi = {
  // F-017-05 / F-103-03 (Bug-9091): consumption surfaces (Model Health
  // scorecard, viewers) pass deployedOnly so a certified-but-undeployed edit
  // never changes the executive card before Deploy — the served definition then
  // comes from the deployed snapshot, matching JDBC/XMLA. The model builder omits
  // the flag and keeps seeing live drafts.
  list: (projectId: string, modelId: string, personaId?: string, deployedOnly?: boolean) => {
    const params: Record<string, string | boolean> = {};
    if (personaId) params.persona_id = personaId;
    if (deployedOnly) params.deployed_only = true;
    return api
      .get<import("./types").Kpi[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis`,
        Object.keys(params).length ? { params } : undefined,
      )
      .then((r) => r.data);
  },
  create: (projectId: string, modelId: string, data: import("./types").KpiCreate) =>
    api
      .post<import("./types").Kpi>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis`,
        data,
      )
      .then((r) => r.data),
  update: (projectId: string, modelId: string, id: string, data: import("./types").KpiUpdate) =>
    api
      .patch<import("./types").Kpi>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis/${id}`,
        data,
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, id: string) =>
    api.delete(`/api/v1/projects/${projectId}/models/${modelId}/kpis/${id}`),
  evaluate: (projectId: string, modelId: string, id: string) =>
    api
      .post<import("./types").KpiEvaluateResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis/${id}/evaluate`,
      )
      .then((r) => r.data),
  versions: (projectId: string, modelId: string, id: string) =>
    api
      .get<import("./types").VersionEntry[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis/${id}/versions`,
      )
      .then((r) => r.data),
  revert: (projectId: string, modelId: string, id: string, versionNumber: number) =>
    api
      .post<import("./types").Kpi>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis/${id}/versions/${versionNumber}/revert`,
      )
      .then((r) => r.data),
  certify: (projectId: string, modelId: string, id: string) =>
    api
      .post<import("./types").Kpi>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis/${id}/certify`,
        {},
      )
      .then((r) => r.data),
  // F-101-01: publish/unpublish a KPI to the BI catalogues (JDBC $KPIs, XMLA
  // MDSCHEMA_KPIS). is_deployed is a publication flag layered on the deployed
  // model snapshot; the model must be deployed first (backend returns 409).
  deploy: (projectId: string, modelId: string, id: string) =>
    api
      .post<import("./types").Kpi>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis/${id}/deploy`,
        {},
      )
      .then((r) => r.data),
  undeploy: (projectId: string, modelId: string, id: string) =>
    api
      .post<import("./types").Kpi>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis/${id}/undeploy`,
        {},
      )
      .then((r) => r.data),
  deprecate: (projectId: string, modelId: string, id: string, data: import("./types").DeprecateRequest) =>
    api
      .post<import("./types").Kpi>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis/${id}/deprecate`,
        data,
      )
      .then((r) => r.data),
  reportUsage: (projectId: string, modelId: string, id: string, data: import("./types").EntityUsageCreate) =>
    api
      .post(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis/${id}/usage`,
        data,
      )
      .then((r) => r.data),
  listUsage: (projectId: string, modelId: string, id: string) =>
    api
      .get<import("./types").EntityUsageEntry[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis/${id}/usage`,
      )
      .then((r) => r.data),

  // v2 methods
  validateExpression: (
    projectId: string,
    modelId: string,
    data: import("./types").KpiValidateExpressionRequest,
  ) =>
    api
      .post<import("./types").KpiValidationResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis/validate-expression`,
        data,
      )
      .then((r) => r.data),

  evaluateBatch: (
    projectId: string,
    modelId: string,
    data: import("./types").KpiBatchRequest,
    personaId?: string,
  ) =>
    api
      .post<import("./types").KpiBatchResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis/evaluate-batch`,
        data,
        // F-017-25: persona switcher on the scorecard re-evaluates under the
        // selected persona's measure scope (endpoint accepts persona_id query).
        // The scorecard evaluates only the deployed KPI ids from the
        // deployed_only list (F-017-05), and a deployed model pins each KPI to
        // its snapshot definition server-side, so batch needs no separate flag.
        personaId ? { params: { persona_id: personaId } } : undefined,
      )
      .then((r) => r.data),

  evaluateAdhoc: (
    projectId: string,
    modelId: string,
    data: import("./types").KpiAdhocRequest,
  ) =>
    api
      .post<import("./types").KpiEvaluateResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis/evaluate-adhoc`,
        data,
      )
      .then((r) => r.data),

  getTrendSeries: (projectId: string, modelId: string, id: string) =>
    api
      .get<import("./types").KpiTrendPoint[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/kpis/${id}/trend-series`,
      )
      .then((r) => r.data),
};

export const preferencesApi = {
  get: (projectId: string, modelId: string) =>
    api
      .get<import("./types").UserPreferencesResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/preferences`,
      )
      .then((r) => r.data),
  toggleFavourite: (projectId: string, modelId: string, data: import("./types").UserPreferenceToggle) =>
    api
      .put<{ favourited: boolean }>(
        `/api/v1/projects/${projectId}/models/${modelId}/preferences/favourite`,
        data,
      )
      .then((r) => r.data),
  recordRecentlyUsed: (projectId: string, modelId: string, data: import("./types").UserPreferenceToggle) =>
    api
      .post<{ recorded: boolean }>(
        `/api/v1/projects/${projectId}/models/${modelId}/preferences/recently-used`,
        data,
      )
      .then((r) => r.data),
  /** Bug-8183: the whole project's favourited models in one request. */
  getFavouriteModels: (projectId: string) =>
    api
      .get<import("./types").FavouriteModelsResponse>(
        `/api/v1/projects/${projectId}/preferences/favourite-models`,
      )
      .then((r) => r.data),
};

export type PivotSort = {
  measure: {
    measureId: string;
    aggregation: string;
    occurrence: number;
  };
  target:
    | { kind: "column"; columnKey: string[] }
    | { kind: "grand" };
  direction: "asc" | "desc";
};

export interface PivotViewConfig extends Record<string, unknown> {
  configVersion?: 2;
  sort?: PivotSort | null;
}

export type PivotSortConfigIssue = "invalid-sort" | "unsupported-version";

export type PivotSortConfigResult = {
  sort: PivotSort | null;
  issue: PivotSortConfigIssue | null;
};

export function buildPivotViewConfig(
  config: Omit<PivotViewConfig, "configVersion" | "sort">,
  sort: PivotSort | null,
): PivotViewConfig {
  return { configVersion: 2, ...config, sort };
}

export function parsePivotSort(value: unknown): PivotSort | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const candidate = value as Record<string, unknown>;
  const measure = candidate.measure;
  const target = candidate.target;
  if (!measure || typeof measure !== "object" || Array.isArray(measure)) return null;
  if (!target || typeof target !== "object" || Array.isArray(target)) return null;
  const m = measure as Record<string, unknown>;
  const t = target as Record<string, unknown>;
  if (
    typeof m.measureId !== "string" ||
    !m.measureId ||
    typeof m.aggregation !== "string" ||
    !m.aggregation ||
    !Number.isInteger(m.occurrence) ||
    (m.occurrence as number) < 0 ||
    (candidate.direction !== "asc" && candidate.direction !== "desc")
  ) return null;
  if (t.kind === "column") {
    if (!Array.isArray(t.columnKey) || !t.columnKey.every((part) => typeof part === "string")) {
      return null;
    }
  } else if (t.kind !== "grand") {
    return null;
  }
  return {
    measure: {
      measureId: m.measureId,
      aggregation: m.aggregation.toUpperCase(),
      occurrence: m.occurrence as number,
    },
    target: t.kind === "grand"
      ? { kind: "grand" }
      : { kind: "column", columnKey: [...(t.columnKey as string[])] },
    direction: candidate.direction,
  };
}

export function parsePivotViewSortConfig(config: unknown): PivotSortConfigResult {
  if (!config || typeof config !== "object" || Array.isArray(config)) {
    return { sort: null, issue: "invalid-sort" };
  }
  const candidate = config as Record<string, unknown>;
  const version = candidate.configVersion;
  if (version !== undefined && version !== 2) {
    return { sort: null, issue: "unsupported-version" };
  }
  if (candidate.sort === undefined || candidate.sort === null) {
    return { sort: null, issue: null };
  }
  if (version !== 2) {
    return { sort: null, issue: "unsupported-version" };
  }
  const sort = parsePivotSort(candidate.sort);
  return sort
    ? { sort, issue: null }
    : { sort: null, issue: "invalid-sort" };
}

export interface PivotView {
  id: string;
  model_id: string;
  name: string;
  measure_id: string;
  row_dim_ids: string[];
  col_dim_ids: string[];
  config: PivotViewConfig | null;
  created_by: string;
  /** Whether the view is visible to the whole tenant (F-029-22). */
  is_shared: boolean;
  /** Whether the requesting user owns this view (drives the share control). */
  is_owner: boolean;
  /**
   * Whether the caller may edit/delete this view. True for the owner, and for
   * a modeler+ on a shared view they do not own (Bug-5839).
   */
  can_edit: boolean;
  created_at: string;
  updated_at: string;
}

export const pivotViewsApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<PivotView[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/pivot-views`,
      )
      .then((r) => r.data),
  create: (
    projectId: string,
    modelId: string,
    data: {
      name: string;
      measure_id: string;
      row_dim_ids: string[];
      col_dim_ids: string[];
      config?: PivotViewConfig;
      is_shared?: boolean;
    },
  ) =>
    api
      .post<PivotView>(
        `/api/v1/projects/${projectId}/models/${modelId}/pivot-views`,
        data,
      )
      .then((r) => r.data),
  update: (
    projectId: string,
    modelId: string,
    viewId: string,
    data: Partial<{
      name: string;
      measure_id: string;
      row_dim_ids: string[];
      col_dim_ids: string[];
      config: PivotViewConfig;
      is_shared: boolean;
    }>,
  ) =>
    api
      .patch<PivotView>(
        `/api/v1/projects/${projectId}/models/${modelId}/pivot-views/${viewId}`,
        data,
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, viewId: string) =>
    api
      .delete(
        `/api/v1/projects/${projectId}/models/${modelId}/pivot-views/${viewId}`,
      )
      .then((r) => r.data),
};

export const impactScanApi = {
  scan: (projectId: string, modelId: string) =>
    api
      .post<ImpactScanResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/impact/scan`
      )
      .then((r) => r.data),
  queryReferences: (projectId: string, modelId: string) =>
    api
      .get<GatewayQueryReference[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/impact/query-references`
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Impact Analysis (Bug-7787, Phase 4)
// ---------------------------------------------------------------------------

export const impactAnalysisApi = {
  catalogue: (
    projectId: string,
    modelId: string,
    params?: { object_types?: string; search?: string; cursor?: string; limit?: number },
  ) =>
    api
      .get<import("./types_domains/model_impact").ImpactCatalogueResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/impact-analysis/objects`,
        { params },
      )
      .then((r) => r.data),

  query: (
    projectId: string,
    modelId: string,
    body: import("./types_domains/model_impact").ImpactQueryRequest,
  ) =>
    api
      .post<import("./types_domains/model_impact").ImpactResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/impact-analysis/query`,
        body,
      )
      .then((r) => r.data),

  columnUsage: (projectId: string, modelId: string) =>
    api
      .get<import("./types_domains/governance_impact").ColumnUsageResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/impact/column-usage`,
      )
      .then((r) => r.data),
};

export const savedQueriesApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<import("./types").SavedQuery[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/saved-queries`
      )
      .then((r) => r.data),
  get: (projectId: string, modelId: string, queryId: string) =>
    api
      .get<import("./types").SavedQuery>(
        `/api/v1/projects/${projectId}/models/${modelId}/saved-queries/${queryId}`
      )
      .then((r) => r.data),
  create: (
    projectId: string,
    modelId: string,
    data: import("./types").SavedQueryCreate
  ) =>
    api
      .post<import("./types").SavedQuery>(
        `/api/v1/projects/${projectId}/models/${modelId}/saved-queries`,
        data
      )
      .then((r) => r.data),
  update: (
    projectId: string,
    modelId: string,
    queryId: string,
    data: import("./types").SavedQueryUpdate
  ) =>
    api
      .patch<import("./types").SavedQuery>(
        `/api/v1/projects/${projectId}/models/${modelId}/saved-queries/${queryId}`,
        data
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, queryId: string) =>
    api
      .delete(
        `/api/v1/projects/${projectId}/models/${modelId}/saved-queries/${queryId}`
      )
      .then((r) => r.data),
};

export type TranslationRecord = {
  id: string;
  model_id: string;
  entity_type: string;
  entity_id: string;
  field_name: string;
  locale: string;
  translated_text: string;
  source: string;
};

export const translationsApi = {
  list: (
    projectId: string,
    modelId: string,
    params?: { locale?: string; entity_type?: string; entity_id?: string },
  ) =>
    api
      .get<TranslationRecord[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/translations`,
        { params },
      )
      .then((r) => r.data),
  create: (
    projectId: string,
    modelId: string,
    data: {
      entity_type: string;
      entity_id: string;
      field_name: string;
      locale: string;
      translated_text: string;
      source?: string;
    },
  ) =>
    api
      .post<TranslationRecord>(
        `/api/v1/projects/${projectId}/models/${modelId}/translations`,
        data,
      )
      .then((r) => r.data),
  bulkUpsert: (
    projectId: string,
    modelId: string,
    translations: Array<{
      entity_type: string;
      entity_id: string;
      field_name: string;
      locale: string;
      translated_text: string;
      source?: string;
    }>,
  ) =>
    api
      .post<TranslationRecord[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/translations/bulk`,
        { translations },
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, translationId: string) =>
    api
      .delete(
        `/api/v1/projects/${projectId}/models/${modelId}/translations/${translationId}`,
      )
      .then((r) => r.data),
  exportUrl: (projectId: string, modelId: string, format: "csv" | "json" = "csv", locale?: string) => {
    const params = new URLSearchParams({ format });
    if (locale) params.set("locale", locale);
    return `/api/v1/projects/${projectId}/models/${modelId}/translations/export?${params}`;
  },
  importFile: (projectId: string, modelId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api
      .post<{ imported: number; skipped: number; errors: string[] }>(
        `/api/v1/projects/${projectId}/models/${modelId}/translations/import`,
        form,
        { headers: { "Content-Type": "multipart/form-data" } },
      )
      .then((r) => r.data);
  },
  coverage: (projectId: string, modelId: string) =>
    api
      .get<{
        total_translatable: number;
        locales: Array<{ locale: string; translated: number; total: number; percent: number }>;
      }>(
        `/api/v1/projects/${projectId}/models/${modelId}/translations/coverage`,
      )
      .then((r) => r.data),
  bootstrap: (projectId: string, modelId: string, targetLocale: string) =>
    api
      .post<{ proposed: number; errors: string[] }>(
        `/api/v1/projects/${projectId}/models/${modelId}/translations/bootstrap`,
        { target_locale: targetLocale },
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Tenant Branding
// ---------------------------------------------------------------------------

export interface BrandingConfig {
  logo_url: string | null;
  primary_color: string | null;
  secondary_color: string | null;
  font_family: string | null;
  app_title: string | null;
}

export const brandingApi = {
  get: (tenantId: string) =>
    api
      .get<BrandingConfig>(`/api/v1/tenants/${encodeURIComponent(tenantId)}/branding`)
      .then((r) => r.data),
  update: (tenantId: string, data: BrandingConfig) =>
    api
      .put<BrandingConfig>(
        `/api/v1/tenants/${encodeURIComponent(tenantId)}/branding`,
        data,
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Scratchpad Measures (per-user ephemeral calculated expressions)
// ---------------------------------------------------------------------------

export const scratchpadApi = {
  list: (projectId: string, modelId: string) =>
    api
      .get<import("./types").ScratchpadMeasure[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/scratchpad-measures`,
      )
      .then((r) => r.data),
  create: (projectId: string, modelId: string, data: import("./types").ScratchpadMeasureCreate) =>
    api
      .post<import("./types").ScratchpadMeasure>(
        `/api/v1/projects/${projectId}/models/${modelId}/scratchpad-measures`,
        data,
      )
      .then((r) => r.data),
  update: (projectId: string, modelId: string, measureId: string, data: import("./types").ScratchpadMeasureUpdate) =>
    api
      .patch<import("./types").ScratchpadMeasure>(
        `/api/v1/projects/${projectId}/models/${modelId}/scratchpad-measures/${measureId}`,
        data,
      )
      .then((r) => r.data),
  delete: (projectId: string, modelId: string, measureId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/scratchpad-measures/${measureId}`,
    ),
};

// ---------------------------------------------------------------------------
// Model Documentation (auto-generated markdown docs)
// ---------------------------------------------------------------------------

export const modelDocsApi = {
  generate: (projectId: string, modelId: string) =>
    api
      .get<import("./types").ModelDocsResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/docs/generate`,
      )
      .then((r) => r.data),
  markdown: (projectId: string, modelId: string) =>
    api
      .get<string>(
        `/api/v1/projects/${projectId}/models/${modelId}/docs/markdown`,
        { responseType: "text", transformResponse: [(d: string) => d] },
      )
      .then((r) => r.data),
};

// ---------------------------------------------------------------------------
// Auto-Classification (apply ML classification to table columns)
// ---------------------------------------------------------------------------

export const autoClassifyApi = {
  apply: (
    projectId: string,
    modelId: string,
    sourceId: string,
    tableId: string,
  ) =>
    api
      .post<{ applied: number; skipped: number }>(
        `/api/v1/projects/${projectId}/models/${modelId}/sources/${sourceId}/tables/${tableId}/apply-classification`,
      )
      .then((r) => r.data),
};

export const solidatusApi = {
  listConfigs: (projectId: string, modelId: string) =>
    api
      .get<import("./types").SolidatusConnection[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/solidatus/config`
      )
      .then((r) => r.data),
  createConfig: (
    projectId: string,
    modelId: string,
    data: import("./types").SolidatusConnectionCreate
  ) =>
    api
      .post<import("./types").SolidatusConnection>(
        `/api/v1/projects/${projectId}/models/${modelId}/solidatus/config`,
        data
      )
      .then((r) => r.data),
  updateConfig: (
    projectId: string,
    modelId: string,
    connectionId: string,
    data: import("./types").SolidatusConnectionUpdate
  ) =>
    api
      .put<import("./types").SolidatusConnection>(
        `/api/v1/projects/${projectId}/models/${modelId}/solidatus/config/${connectionId}`,
        data
      )
      .then((r) => r.data),
  deleteConfig: (projectId: string, modelId: string, connectionId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/solidatus/config/${connectionId}`
    ),
  validate: (
    projectId: string,
    modelId: string,
    data: import("./types").SolidatusValidateRequest
  ) =>
    api
      .post<import("./types").SolidatusValidateResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/solidatus/validate`,
        data
      )
      .then((r) => r.data),
  exportPreview: (
    projectId: string,
    modelId: string,
    data: import("./types").SolidatusExportPreviewRequest
  ) =>
    api
      .post<import("./types").SolidatusExportPreviewResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/solidatus/export-preview`,
        data
      )
      .then((r) => r.data),
  sync: (
    projectId: string,
    modelId: string,
    data: import("./types").SolidatusSyncRequest
  ) =>
    api
      .post<import("./types").SolidatusSyncResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/solidatus/sync`,
        data
      )
      .then((r) => r.data),
  listRuns: (projectId: string, modelId: string) =>
    api
      .get<import("./types").SolidatusSyncRun[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/solidatus/runs`
      )
      .then((r) => r.data),
  getRun: (projectId: string, modelId: string, runId: string) =>
    api
      .get<import("./types").SolidatusSyncRun>(
        `/api/v1/projects/${projectId}/models/${modelId}/solidatus/runs/${runId}`
      )
      .then((r) => r.data),
  listMappings: (projectId: string, modelId: string) =>
    api
      .get<import("./types").SolidatusObjectMapping[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/solidatus/mappings`
      )
      .then((r) => r.data),
};

export const collibraApi = {
  listConfigs: (projectId: string, modelId: string) =>
    api
      .get<import("./types").CollibraConnection[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/collibra/config`
      )
      .then((r) => r.data),
  createConfig: (
    projectId: string,
    modelId: string,
    data: import("./types").CollibraConnectionCreate
  ) =>
    api
      .post<import("./types").CollibraConnection>(
        `/api/v1/projects/${projectId}/models/${modelId}/collibra/config`,
        data
      )
      .then((r) => r.data),
  updateConfig: (
    projectId: string,
    modelId: string,
    connectionId: string,
    data: import("./types").CollibraConnectionUpdate
  ) =>
    api
      .put<import("./types").CollibraConnection>(
        `/api/v1/projects/${projectId}/models/${modelId}/collibra/config/${connectionId}`,
        data
      )
      .then((r) => r.data),
  deleteConfig: (projectId: string, modelId: string, connectionId: string) =>
    api.delete(
      `/api/v1/projects/${projectId}/models/${modelId}/collibra/config/${connectionId}`
    ),
  validate: (
    projectId: string,
    modelId: string,
    data: import("./types").CollibraValidateRequest
  ) =>
    api
      .post<import("./types").CollibraValidateResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/collibra/validate`,
        data
      )
      .then((r) => r.data),
  exportPreview: (
    projectId: string,
    modelId: string,
    data: import("./types").CollibraExportPreviewRequest
  ) =>
    api
      .post<import("./types").CollibraExportPreviewResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/collibra/export-preview`,
        data
      )
      .then((r) => r.data),
  sync: (
    projectId: string,
    modelId: string,
    data: import("./types").CollibraSyncRequest
  ) =>
    api
      .post<import("./types").CollibraSyncResponse>(
        `/api/v1/projects/${projectId}/models/${modelId}/collibra/sync`,
        data
      )
      .then((r) => r.data),
  listRuns: (projectId: string, modelId: string) =>
    api
      .get<import("./types").CollibraSyncRun[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/collibra/runs`
      )
      .then((r) => r.data),
  getRun: (projectId: string, modelId: string, runId: string) =>
    api
      .get<import("./types").CollibraSyncRun>(
        `/api/v1/projects/${projectId}/models/${modelId}/collibra/runs/${runId}`
      )
      .then((r) => r.data),
  listMappings: (projectId: string, modelId: string) =>
    api
      .get<import("./types").CollibraObjectMapping[]>(
        `/api/v1/projects/${projectId}/models/${modelId}/collibra/mappings`
      )
      .then((r) => r.data),
};

export default api;
