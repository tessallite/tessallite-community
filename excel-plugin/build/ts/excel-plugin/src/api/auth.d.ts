import type { LoginRequest, LoginResponse, UserInfo } from '../types/tessallite';
export declare function login(req: LoginRequest): Promise<LoginResponse>;
export declare function logout(): Promise<void>;
export declare function getCurrentUser(): Promise<UserInfo>;
export declare function getTenantInfo(): Promise<{
    id: string;
    name: string;
    slug: string;
}>;
