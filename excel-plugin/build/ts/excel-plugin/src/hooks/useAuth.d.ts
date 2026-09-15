import { ConnectionProfile } from '../utils/storage';
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
export declare function useAuth(): UseAuthReturn;
export {};
