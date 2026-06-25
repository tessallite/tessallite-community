/**
 * Storage abstraction — uses OfficeRuntime.storage exclusively.
 * localStorage is NOT used because JWTs stored there are accessible to
 * any JavaScript in the same origin (XSS vector).
 * Never stores passwords.
 */

declare const OfficeRuntime: {
  storage: {
    getItem(key: string): Promise<string | null>;
    setItem(key: string, value: string): Promise<void>;
    removeItem(key: string): Promise<void>;
  };
};

interface StorageBackend {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  removeItem(key: string): Promise<void>;
}

class StorageUnavailableError extends Error {
  constructor() {
    super('Secure storage unavailable. OfficeRuntime.storage is required. Please upgrade to a supported version of Office.');
    this.name = 'StorageUnavailableError';
  }
}

function getBackend(): StorageBackend {
  if (typeof OfficeRuntime !== 'undefined' && OfficeRuntime?.storage) {
    return OfficeRuntime.storage;
  }
  return {
    getItem: () => Promise.reject(new StorageUnavailableError()),
    setItem: () => Promise.reject(new StorageUnavailableError()),
    removeItem: () => Promise.reject(new StorageUnavailableError()),
  };
}

function storage(): StorageBackend {
  return getBackend();
}

const STORAGE_KEYS = {
  JWT: 'tessallite_jwt',
  PROFILES: 'tessallite_profiles',
  ACTIVE_PROFILE_ID: 'tessallite_active_profile',
  SESSION_PROFILE: 'tessallite_session_profile',
  LAST_MODE: 'tessallite_last_mode',
  PROJECT_ID: 'tessallite_project_id',
  MODEL_ID: 'tessallite_model_id',
  // F-025-17: the active "Viewing as <persona>" selection, persisted so the
  // custom-functions runtime (a separate JS context that cannot read pane
  // React state) can scope KPI evaluations to the same persona the pane shows.
  ACTIVE_PERSONA_ID: 'tessallite_active_persona',
} as const;

export interface ConnectionProfile {
  id: string;
  name: string;
  serverUrl: string;
  tenantId: string;
  email: string;
  createdAt: string;
}

export async function getJwt(): Promise<string | null> {
  try {
    const stored = await storage().getItem(STORAGE_KEYS.JWT);
    return stored || null;
  } catch {
    return null;
  }
}

export async function setJwt(jwt: string): Promise<void> {
  await storage().setItem(STORAGE_KEYS.JWT, jwt);
}

export async function removeJwt(): Promise<void> {
  await storage().removeItem(STORAGE_KEYS.JWT);
}

export async function getProfiles(): Promise<ConnectionProfile[]> {
  try {
    const stored = await storage().getItem(STORAGE_KEYS.PROFILES);
    return stored ? JSON.parse(stored) : [];
  } catch {
    return [];
  }
}

export async function saveProfile(profile: ConnectionProfile): Promise<void> {
  const profiles = await getProfiles();
  const existing = profiles.findIndex(p => p.id === profile.id);
  if (existing >= 0) {
    profiles[existing] = profile;
  } else {
    profiles.push(profile);
  }
  await storage().setItem(STORAGE_KEYS.PROFILES, JSON.stringify(profiles));
}

export async function removeProfile(profileId: string): Promise<void> {
  const profiles = await getProfiles();
  const filtered = profiles.filter(p => p.id !== profileId);
  await storage().setItem(STORAGE_KEYS.PROFILES, JSON.stringify(filtered));
}

export async function getActiveProfile(): Promise<ConnectionProfile | null> {
  try {
    const session = await storage().getItem(STORAGE_KEYS.SESSION_PROFILE);
    if (session) return JSON.parse(session) as ConnectionProfile;

    const id = await storage().getItem(STORAGE_KEYS.ACTIVE_PROFILE_ID);
    if (!id) return null;
    const profiles = await getProfiles();
    return profiles.find(p => p.id === id) || null;
  } catch {
    return null;
  }
}

export async function setActiveProfile(profileId: string): Promise<void> {
  await storage().setItem(STORAGE_KEYS.ACTIVE_PROFILE_ID, profileId);
}

export async function setSessionProfile(profile: ConnectionProfile): Promise<void> {
  await storage().setItem(STORAGE_KEYS.SESSION_PROFILE, JSON.stringify(profile));
}

export async function clearSessionProfile(): Promise<void> {
  await storage().removeItem(STORAGE_KEYS.SESSION_PROFILE);
}

export async function getLastMode(): Promise<string | null> {
  try {
    return await storage().getItem(STORAGE_KEYS.LAST_MODE);
  } catch {
    return null;
  }
}

export async function setLastMode(mode: string): Promise<void> {
  await storage().setItem(STORAGE_KEYS.LAST_MODE, mode);
}

export async function getModelContext(): Promise<{ projectId: string; modelId: string } | null> {
  try {
    const projectId = await storage().getItem(STORAGE_KEYS.PROJECT_ID);
    const modelId = await storage().getItem(STORAGE_KEYS.MODEL_ID);
    if (projectId && modelId) return { projectId, modelId };
    return null;
  } catch {
    return null;
  }
}

export async function setModelContext(projectId: string, modelId: string): Promise<void> {
  await storage().setItem(STORAGE_KEYS.PROJECT_ID, projectId);
  await storage().setItem(STORAGE_KEYS.MODEL_ID, modelId);
}

export async function clearModelContext(): Promise<void> {
  await storage().removeItem(STORAGE_KEYS.PROJECT_ID);
  await storage().removeItem(STORAGE_KEYS.MODEL_ID);
}

// F-025-17: persist the active persona so the custom-functions runtime can
// evaluate TESS.* KPIs under the same "Viewing as" persona as the task pane.
// A null/empty selection clears it (back to the user's default persona).
export async function getActivePersonaId(): Promise<string | null> {
  try {
    const stored = await storage().getItem(STORAGE_KEYS.ACTIVE_PERSONA_ID);
    return stored || null;
  } catch {
    return null;
  }
}

export async function setActivePersonaId(personaId: string | null): Promise<void> {
  if (personaId) {
    await storage().setItem(STORAGE_KEYS.ACTIVE_PERSONA_ID, personaId);
  } else {
    await storage().removeItem(STORAGE_KEYS.ACTIVE_PERSONA_ID);
  }
}

export async function clearSessionData(): Promise<void> {
  await Promise.all([
    removeJwt(),
    clearSessionProfile(),
    storage().removeItem(STORAGE_KEYS.ACTIVE_PROFILE_ID),
    clearModelContext(),
    setActivePersonaId(null),
  ]);
}

export async function clearAllAuthData(): Promise<void> {
  await Promise.all([
    clearSessionData(),
    storage().removeItem(STORAGE_KEYS.PROFILES),
  ]);
}
