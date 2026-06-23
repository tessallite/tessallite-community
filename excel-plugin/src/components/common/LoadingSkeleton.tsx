import { Box, Skeleton } from '@mui/material';

interface LoadingSkeletonProps {
  variant: 'card' | 'list' | 'chat' | 'table';
  count?: number;
}

export default function LoadingSkeleton({
  variant,
  count,
}: LoadingSkeletonProps) {
  if (variant === 'card') {
    const n = count ?? 1;
    return (
      <Box sx={{ p: 1 }}>
        {Array.from({ length: n }).map((_, i) => (
          <Skeleton
            key={i}
            variant="rounded"
            height={120}
            width="100%"
            sx={{ mb: 1, borderRadius: 1 }}
          />
        ))}
      </Box>
    );
  }

  if (variant === 'list') {
    const heights = [16, 16, 16, 14, 14];
    const widths = ['90%', '85%', '70%', '80%', '60%'];
    const n = count ?? 5;
    return (
      <Box sx={{ p: 1 }}>
        {Array.from({ length: n }).map((_, i) => (
          <Skeleton
            key={i}
            variant="text"
            width={widths[i % widths.length]}
            height={heights[i % heights.length]}
            sx={{ mb: 0.5 }}
          />
        ))}
      </Box>
    );
  }

  if (variant === 'chat') {
    const n = count ?? 3;
    return (
      <Box sx={{ p: 1.5 }}>
        {Array.from({ length: n }).map((_, i) => {
          const isLeft = i % 2 === 0;
          return (
            <Box key={i} sx={{ display: 'flex', justifyContent: isLeft ? 'flex-start' : 'flex-end', mb: 1 }}>
              <Skeleton
                variant="rounded"
                width={isLeft ? '70%' : '60%'}
                height={48}
                sx={{ borderRadius: 2 }}
              />
            </Box>
          );
        })}
      </Box>
    );
  }

  if (variant === 'table') {
    const n = count ?? 5;
    return (
      <Box sx={{ p: 1 }}>
        <Skeleton variant="rectangular" height={32} width="100%" sx={{ mb: 0.5, borderRadius: 0.5 }} />
        {Array.from({ length: n }).map((_, i) => (
          <Skeleton
            key={i}
            variant="rectangular"
            height={24}
            width="100%"
            sx={{ mb: 0.25, borderRadius: 0.5 }}
          />
        ))}
      </Box>
    );
  }

  return null;
}
