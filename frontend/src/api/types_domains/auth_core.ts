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
export interface User {
  id: string;
  username: string;
  email: string;
  is_active: boolean;
  role: LocalUserRole;
  has_completed_onboarding: boolean;
  created_at: string;
}

export type AccessRole = "admin" | "modeler" | "viewer";

export interface UserAccessBindingCreate {
  user_identity: string;
  role: AccessRole;
  model_id?: string | null;
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

