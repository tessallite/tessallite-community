/**
 * Authentication API.
 */
import { apiClient } from './client';
import type { LoginRequest, LoginResponse, UserInfo } from '../types/tessallite';

export async function login(req: LoginRequest): Promise<LoginResponse> {
  return apiClient.post<LoginResponse>('/api/v1/auth/login', req);
}

export async function logout(): Promise<void> {
  try {
    await apiClient.post('/api/v1/auth/logout');
  } catch {
    // swallow
  }
}

export async function getCurrentUser(): Promise<UserInfo> {
  return apiClient.get<UserInfo>('/api/v1/auth/users/me');
}

export async function getTenantInfo(): Promise<{ id: string; name: string; slug: string }> {
  return apiClient.get('/api/v1/tenants/me');
}

// F-025-21: the duplicate `healthCheck` that lived here was dead — it was
// shadowed by api/gateway.ts:healthCheck (the one App.tsx imports) and never
// referenced. Removed to leave a single health-check implementation.
