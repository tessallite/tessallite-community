/**
 * Authentication hook.
 */
import { useState, useCallback, useEffect } from 'react';
import { login as apiLogin, logout as apiLogout } from '../api/auth';
import { configureApiClient } from '../api/client';
import {
  getJwt, setJwt, removeJwt,
  getProfiles, saveProfile, removeProfile, getActiveProfile, setActiveProfile,
  setSessionProfile, clearSessionData,
  ConnectionProfile,
} from '../utils/storage';

export type AuthState = 'loading' | 'unauthenticated' | 'authenticated';

export interface LoginFormData {
  serverUrl: string;
  tenantId: string;
  email: string;
  password: string;
}

interface UseAuthReturn {
  authState: AuthState;
  profiles: ConnectionProfile[];
  activeProfile: ConnectionProfile | null;
  login: (data: LoginFormData, remember: boolean, profileName?: string) => Promise<void>;
  logout: () => Promise<void>;
  switchProfile: (profileId: string) => Promise<void>;
  deleteProfile: (profileId: string) => Promise<void>;
}

export function useAuth(): UseAuthReturn {
  const [authState, setAuthState] = useState<AuthState>('loading');
  const [profiles, setProfiles] = useState<ConnectionProfile[]>([]);
  const [activeProfile, setActiveProfileState] = useState<ConnectionProfile | null>(null);

  useEffect(() => {
    (async () => {
      const [jwt, storedProfiles, storedActive] = await Promise.all([
        getJwt(),
        getProfiles(),
        getActiveProfile(),
      ]);
      setProfiles(storedProfiles);
      setActiveProfileState(storedActive);
      if (jwt && storedActive) {
        configureApiClient(storedActive.serverUrl, () => setAuthState('unauthenticated'));
        setAuthState('authenticated');
      } else {
        setAuthState('unauthenticated');
      }
    })();
  }, []);

  const login = useCallback(async (data: LoginFormData, remember: boolean, profileName?: string) => {
    const password = data.password;
    configureApiClient(data.serverUrl);

    const response = await apiLogin({
      tenant_id: data.tenantId,
      email: data.email,
      password,
    });

    const token = response.access_token || '';
    await setJwt(token);
    configureApiClient(data.serverUrl, () => setAuthState('unauthenticated'));

    const profile: ConnectionProfile = {
      id: `${data.tenantId}:${data.email}`,
      name: profileName || `${data.email}`,
      serverUrl: data.serverUrl,
      tenantId: data.tenantId,
      email: data.email,
      createdAt: new Date().toISOString(),
    };

    await setSessionProfile(profile);

    if (remember) {
      await saveProfile(profile);
      await setActiveProfile(profile.id);
      setProfiles(await getProfiles());
    }

    setActiveProfileState(profile);

    setAuthState('authenticated');
  }, []);

  const logout = useCallback(async () => {
    try { await apiLogout(); } catch { /* ignore */ }
    await clearSessionData();
    setActiveProfileState(null);
    setAuthState('unauthenticated');
  }, []);

  const switchProfile = useCallback(async (profileId: string) => {
    const profile = (await getProfiles()).find(p => p.id === profileId);
    if (!profile) return;
    await removeJwt();
    await setActiveProfile(profileId);
    configureApiClient(profile.serverUrl, () => setAuthState('unauthenticated'));
    setActiveProfileState(profile);
    setAuthState('unauthenticated');
  }, []);

  const deleteProfileFn = useCallback(async (profileId: string) => {
    await removeProfile(profileId);
    setProfiles(await getProfiles());
  }, []);

  return {
    authState,
    profiles,
    activeProfile,
    login,
    logout,
    switchProfile,
    deleteProfile: deleteProfileFn,
  };
}
