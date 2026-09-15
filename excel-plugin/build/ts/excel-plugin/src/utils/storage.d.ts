/**
 * Storage abstraction — uses OfficeRuntime.storage exclusively.
 * localStorage is NOT used because JWTs stored there are accessible to
 * any JavaScript in the same origin (XSS vector).
 * Never stores passwords.
 */
export interface ConnectionProfile {
    id: string;
    name: string;
    serverUrl: string;
    tenantId: string;
    email: string;
    createdAt: string;
}
export declare function getJwt(): Promise<string | null>;
export declare function setJwt(jwt: string): Promise<void>;
export declare function removeJwt(): Promise<void>;
export declare function getProfiles(): Promise<ConnectionProfile[]>;
export declare function saveProfile(profile: ConnectionProfile): Promise<void>;
export declare function removeProfile(profileId: string): Promise<void>;
export declare function getActiveProfile(): Promise<ConnectionProfile | null>;
export declare function setActiveProfile(profileId: string): Promise<void>;
export declare function setSessionProfile(profile: ConnectionProfile): Promise<void>;
export declare function clearSessionProfile(): Promise<void>;
export declare function getLastMode(): Promise<string | null>;
export declare function setLastMode(mode: string): Promise<void>;
export declare function getModelContext(): Promise<{
    projectId: string;
    modelId: string;
    modelSlug?: string;
    modelName?: string;
} | null>;
export declare function setModelContext(projectId: string, modelId: string, modelSlug?: string, modelName?: string): Promise<void>;
export declare function clearModelContext(): Promise<void>;
export declare function getActivePersonaId(): Promise<string | null>;
export declare function setActivePersonaId(personaId: string | null): Promise<void>;
export declare function clearSessionData(): Promise<void>;
export declare function clearAllAuthData(): Promise<void>;
/**
 * Read the current cache generation token from OfficeRuntime.storage.
 * Returns null if storage is unavailable or no token has been written.
 */
export declare function getCacheGeneration(): Promise<string | null>;
export declare function bumpCacheGeneration(minGeneration?: number): Promise<void>;
export type InsertMode = 'live' | 'static';
/**
 * Read the persisted insert-mode preference. Defaults to 'live' when unset.
 */
export declare function getInsertMode(): Promise<InsertMode>;
/**
 * Persist the insert-mode preference.
 */
export declare function setInsertMode(mode: InsertMode): Promise<void>;
