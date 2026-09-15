export interface StreamCallbacks {
    onEvent: (eventName: string, data: Record<string, unknown>) => void;
    onError: (error: Error) => void;
    onComplete: () => void;
}
/**
 * Lifecycle authority for one logical send.
 *
 * `signal` cancels the transport and `isCurrent` protects callbacks that may
 * arrive after a caller has superseded the send without being able to cancel
 * the underlying promise immediately.
 */
export interface StreamLifecycleOptions {
    signal?: AbortSignal;
    isCurrent?: () => boolean;
}
export interface CompoundStep {
    step_number: number;
    title?: string;
    status: string;
    row_count?: number;
    preview_row?: Record<string, unknown>;
}
export type StreamErrorCode = "http_error" | "timeout" | "unexpected_end" | "connection_lost" | "network_error";
export declare class StreamError extends Error {
    readonly code: StreamErrorCode;
    constructor(code: StreamErrorCode, message: string);
}
export declare function resolveStreamErrorMessage(err: Error, t: (key: string, params?: Record<string, string | number>) => string): string;
