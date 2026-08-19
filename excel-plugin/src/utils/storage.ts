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
  MODEL_SLUG: 'tessallite_model_slug',
  MODEL_NAME: 'tessallite_model_name',
  // F-025-17: the active "Viewing as <persona>" selection, persisted so the
  // custom-functions runtime (a separate JS context that cannot read pane
  // React state) can scope KPI evaluations to the same persona the pane shows.
  ACTIVE_PERSONA_ID: 'tessallite_active_persona',
  // Bug-6912: cross-runtime cache invalidation token. The pane bumps this on
  // Refresh / persona switch / profile switch / logout; the functions runtime
  // compares against its module-level lastSeenGeneration and clears caches on
  // change. Only the CHANGE matters, not ordering.
  CACHE_GENERATION: 'tessallite_cache_generation',
  // Task 3: global insert-mode default (live formulas vs static snapshot).
  INSERT_MODE: 'tessallite_insert_mode',
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

export async function getModelContext(): Promise<{
  projectId: string;
  modelId: string;
  modelSlug?: string;
  modelName?: string;
} | null> {
  try {
    const [projectId, modelId, modelSlug, modelName] = await Promise.all([
      storage().getItem(STORAGE_KEYS.PROJECT_ID),
      storage().getItem(STORAGE_KEYS.MODEL_ID),
      storage().getItem(STORAGE_KEYS.MODEL_SLUG),
      storage().getItem(STORAGE_KEYS.MODEL_NAME),
    ]);
    if (projectId && modelId) {
      return {
        projectId,
        modelId,
        modelSlug: modelSlug || undefined,
        modelName: modelName || undefined,
      };
    }
    return null;
  } catch {
    return null;
  }
}

export async function setModelContext(
  projectId: string,
  modelId: string,
  modelSlug?: string,
  modelName?: string,
): Promise<void> {
  await Promise.all([
    storage().setItem(STORAGE_KEYS.PROJECT_ID, projectId),
    storage().setItem(STORAGE_KEYS.MODEL_ID, modelId),
    modelSlug
      ? storage().setItem(STORAGE_KEYS.MODEL_SLUG, modelSlug)
      : storage().removeItem(STORAGE_KEYS.MODEL_SLUG),
    modelName
      ? storage().setItem(STORAGE_KEYS.MODEL_NAME, modelName)
      : storage().removeItem(STORAGE_KEYS.MODEL_NAME),
  ]);
}

export async function clearModelContext(): Promise<void> {
  await Promise.all([
    storage().removeItem(STORAGE_KEYS.PROJECT_ID),
    storage().removeItem(STORAGE_KEYS.MODEL_ID),
    storage().removeItem(STORAGE_KEYS.MODEL_SLUG),
    storage().removeItem(STORAGE_KEYS.MODEL_NAME),
  ]);
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

// ---------------------------------------------------------------------------
// Bug-6912: Cross-runtime cache generation — bump on refresh/switch/logout
// ---------------------------------------------------------------------------

/**
 * Read the current cache generation token from OfficeRuntime.storage.
 * Returns null if storage is unavailable or no token has been written.
 */
export async function getCacheGeneration(): Promise<string | null> {
  try {
    return await storage().getItem(STORAGE_KEYS.CACHE_GENERATION);
  } catch {
    return null;
  }
}

/**
 * Write a new cache generation token. The functions runtime detects the change
 * and clears its local KPI/list/named-set caches. Value format:
 * `<Date.now() base-36>-<random suffix>` — only CHANGE matters, not ordering.
 */
export async function bumpCacheGeneration(): Promise<void> {
  const token = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
  try {
    await storage().setItem(STORAGE_KEYS.CACHE_GENERATION, token);
  } catch {
    // Storage unavailable — fire-and-forget (tests, degraded hosts).
  }
}

// ---------------------------------------------------------------------------
// Task 3: Global insert-mode default
// ---------------------------------------------------------------------------

export type InsertMode = 'live' | 'static';

/**
 * Read the persisted insert-mode preference. Defaults to 'live' when unset.
 */
export async function getInsertMode(): Promise<InsertMode> {
  try {
    const stored = await storage().getItem(STORAGE_KEYS.INSERT_MODE);
    if (stored === 'static') return 'static';
    return 'live';
  } catch {
    return 'live';
  }
}

/**
 * Persist the insert-mode preference.
 */
export async function setInsertMode(mode: InsertMode): Promise<void> {
  await storage().setItem(STORAGE_KEYS.INSERT_MODE, mode);
}
