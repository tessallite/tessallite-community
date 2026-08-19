// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
export interface LoginRequest {
  tenant_id: string;
  email: string;
  password: string;
}
export interface LoginResponse {
  role?: string;
  tenant_id?: string;
  expires_in?: number;
}
export type LocalUserRole = "member" | "tenant_admin" | "model_technical";

export interface UserCreate {
  username: string;
  email: string;
  password: string;
  role?: LocalUserRole;
  tenant_id?: string;
}
export interface UserUpdate {
  username?: string;
  email?: string;
  is_active?: boolean;
  role?: LocalUserRole;
}
export interface UserPasswordReset {
  password: string;
}
export type RoleSource = "manual" | "sso";

export interface User {
  id: string;
  username: string;
  email: string;
  is_active: boolean;
  role: LocalUserRole;
  // Bug-6597: provenance of `role` — "manual" (operator-set) or "sso" (IdP-derived).
  role_source?: RoleSource;
  has_completed_onboarding: boolean;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Personal Access Tokens (Bug-7314) — BI-client auth for SSO users
// ---------------------------------------------------------------------------
export interface PersonalAccessTokenCreate {
  label?: string;
  // Optional lifetime in days (1..365). Omit for a non-expiring token.
  expires_in_days?: number | null;
}

export interface PersonalAccessToken {
  id: string;
  label: string;
  token_prefix: string;
  created_at: string;
  expires_at?: string | null;
  last_used_at?: string | null;
  revoked_at?: string | null;
}

export interface PersonalAccessTokenCreateResponse {
  // The plaintext PAT — shown ONCE at creation, never retrievable again.
  token: string;
  pat: PersonalAccessToken;
}

// Bug-8101 / F-104-01: model_viewer is the built-in read-only consumer role
// (viewer-level privilege). Modeller supersedes it (mutual exclusivity).
export type AccessRole = "admin" | "modeler" | "viewer" | "model_viewer";

export interface UserAccessBindingCreate {
  user_identity: string;
  role: AccessRole;
  model_id?: string | null;
}

// Bug-8101: dry-run response for the Modeller/Model-viewer supersession check.
export interface AccessSupersedePreflightResponse {
  supersedes: boolean;
  removed_model_viewer_count: number;
  grant_is_redundant: boolean;
}

export interface UserAccessBinding {
  id: string;
  project_id: string | null;
  model_id: string | null;
  user_identity: string;
  role: AccessRole;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Tenants
// ---------------------------------------------------------------------------
export interface TenantCreate {
  slug: string;
  display_name: string;
}
export interface TenantUpdate {
  display_name?: string;
  is_active?: boolean;
}
export interface Tenant {
  id: string;
  slug: string;
  display_name: string;
  db_schema_prefix?: string;
  is_active: boolean;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------
export interface ProjectCreate {
  slug: string;
  display_name?: string;
  pocket_size_budget_bytes?: number | null;
}
export interface ProjectUpdate {
  slug?: string;
  display_name?: string;
  is_active?: boolean;
  pocket_size_budget_bytes?: number | null;
}
export interface Project {
  id: string;
  slug: string;
  display_name: string;
  is_active: boolean;
  pocket_size_budget_bytes?: number | null;
  created_at: string;
}

