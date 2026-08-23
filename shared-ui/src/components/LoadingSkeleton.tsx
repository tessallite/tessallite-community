import { Box, Skeleton, Stack } from "@mui/material";

export function LoadingSkeleton() {
  return (
    <Stack spacing={2} sx={{ p: 2 }}>
      {[1, 2, 3].map((n) => (
        <Box key={n}>
          <Box sx={{ display: "flex", justifyContent: "flex-end", mb: 1 }}>
            <Skeleton variant="rounded" width="40%" height={36} />
          </Box>
          <Skeleton variant="rounded" width="80%" height={72} />
        </Box>
      ))}
    </Stack>
  );
}
