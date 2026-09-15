/**
 * Core API client for Tessallite services.
 * All calls use JWT bearer authentication via OfficeRuntime.storage.
 */
export declare function configureApiClient(serverUrl: string, on401?: () => void): void;
export declare class ApiError extends Error {
    status: number;
    body: unknown;
    constructor(status: number, body: unknown);
}
export interface RequestOptions {
    signal?: AbortSignal;
}
declare function safeGet<T>(path: string, options?: RequestOptions): Promise<T>;
export declare const apiClient: {
    get: typeof safeGet;
    post: <T>(path: string, body?: unknown) => Promise<T>;
    put: <T>(path: string, body?: unknown) => Promise<T>;
    patch: <T>(path: string, body?: unknown) => Promise<T>;
    delete: <T>(path: string) => Promise<T>;
};
export declare function streamRequest(path: string, body: unknown, signal?: AbortSignal, extraHeaders?: Record<string, string>): Promise<Response>;
export declare function formatApiError(err: ApiError): string;
export {};
