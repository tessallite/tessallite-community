/**
 * Client-side diagnostics.
 * Keeps last 100 events for support copy/paste.
 * Redacts passwords, JWTs, and connection strings.
 */
export declare function logApiEvent(route: string, status: number, durationMs: number): void;
export declare function logExcelEvent(operation: string, error?: string): void;
export declare function logError(message: string): void;
export declare function logInfo(message: string): void;
export declare function getDiagnosticsReport(): string;
export declare function copyDiagnostics(): void;
export declare function clearDiagnostics(): void;
