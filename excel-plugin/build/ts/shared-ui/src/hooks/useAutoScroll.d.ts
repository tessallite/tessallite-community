export declare function useAutoScroll(deps?: unknown[]): {
    containerRef: import("react").RefObject<HTMLDivElement>;
    isNearBottom: boolean;
    scrollToBottom: () => void;
    showScrollButton: boolean;
};
