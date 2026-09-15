/**
 * All shared-chat keys this plugin resolves. Exported so the parity test can
 * assert (structurally, against the canonical shared-chat.json) that every
 * canonical key is covered and correctly classified — replacing the former
 * hand-maintained key list that silently drifted (deep-review finding).
 */
export declare const CHAT_STRING_KEYS: ReadonlySet<string>;
export declare function chatT(key: string, params?: Record<string, string | number>): string;
