export interface TestProfileDefinition {
    serverUrl: string;
    tenantId: string;
    email: string;
    password: string;
    /** Project slug or display name, resolved to an id at start-up. */
    project: string;
    /** Model slug or display name, resolved to an id at start-up. */
    model: string;
}
/**
 * Stamped into the bundle and shown in the pane header. A build carrying this
 * string is a test build and can never be mistaken for a shipping one.
 */
export declare const TEST_BUILD_MARKER = "TESSALLITE TEST BUILD - NOT FOR RELEASE";
export declare const isTestProfileBuild: boolean;
/** The reason the preset sign-in failed, for the header marker. Null when fine. */
export declare function testProfileFailure(): string | null;
/**
 * The project the pane should open on. Returns `fallback` unchanged outside a
 * test build, so the shipping bootstrap is untouched.
 */
export declare function preferredProject<T extends {
    id: string;
}>(projects: T[], fallback: T): T;
/** The model the pane should open on. `fallback` unchanged outside a test build. */
export declare function preferredModel<T extends {
    id: string;
}>(models: T[], fallback: T): T;
/**
 * Sign in from the baked profile and seed the storage keys a signed-in pane
 * writes. Resolves either way: a failure is reported through the header marker
 * rather than thrown, so the pane still renders and says what went wrong.
 */
export declare function applyTestProfile(): Promise<void>;
