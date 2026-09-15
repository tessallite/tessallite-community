import { Box, Skeleton, Stack } from "@mui/material";

export function LoadingSkeleton({ compact = false }: { compact?: boolean }) {
  return (
    <Stack spacing={compact ? 1 : 2} sx={{ p: compact ? 1 : 2 }}>
      {[1, 2, 3].map((n) => (
        <Box key={n}>
          <Box sx={{ display: "flex", justifyContent: "flex-end", mb: compact ? 0.5 : 1 }}>
            <Skeleton variant="rounded" width="40%" height={compact ? 28 : 36} />
          </Box>
          <Skeleton variant="rounded" width="80%" height={compact ? 56 : 72} />
        </Box>
      ))}
    </Stack>
  );
}
