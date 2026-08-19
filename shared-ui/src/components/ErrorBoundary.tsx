import { Component, type ErrorInfo, type ReactNode } from "react";
import { Box, Typography } from "@mui/material";

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
}

export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(_error: Error, _info: ErrorInfo) {
    // Errors are silently caught — the fallback renders in place of the
    // broken subtree. No telemetry endpoint exists at this layer.
  }

  render() {
    if (this.state.hasError) {
      return (
        this.props.fallback ?? (
          <Box sx={{ p: 2 }}>
            <Typography variant="body2" color="error">
              Something went wrong rendering this block.
            </Typography>
          </Box>
        )
      );
    }
    return this.props.children;
  }
}
