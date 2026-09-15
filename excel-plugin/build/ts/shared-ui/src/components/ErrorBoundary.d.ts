import { Component, type ErrorInfo, type ReactNode } from "react";
interface ErrorBoundaryProps {
    children: ReactNode;
    fallback?: ReactNode;
}
interface ErrorBoundaryState {
    hasError: boolean;
}
export declare class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
    constructor(props: ErrorBoundaryProps);
    static getDerivedStateFromError(): ErrorBoundaryState;
    componentDidCatch(_error: Error, _info: ErrorInfo): void;
    render(): string | number | boolean | Iterable<ReactNode> | import("react").JSX.Element | null | undefined;
}
export {};
