/**
 * Serialises governed project/model transitions in the task pane.
 *
 * A generation is both the callback authority and the cancellation boundary:
 * beginning a newer transition aborts the older fetch and makes every older
 * state/storage/cache/recalculation continuation obsolete. The queue keeps
 * OfficeRuntime writes and workbook recalculations in order when an older
 * operation was already in flight when the newer transition began.
 */
export interface ContextTransition {
    readonly generation: number;
    readonly signal: AbortSignal;
    isCurrent: () => boolean;
}
export interface ContextTransitionCoordinator {
    begin: () => ContextTransition;
    currentGeneration: () => number;
    isCurrent: (generation: number) => boolean;
    run: <T>(transition: ContextTransition, work: (transition: ContextTransition) => Promise<T>) => Promise<T | undefined>;
    cancel: () => void;
}
export declare function createContextTransitionCoordinator(): ContextTransitionCoordinator;
