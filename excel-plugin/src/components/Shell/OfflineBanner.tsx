import { Box, Typography } from '@mui/material';
import { tokens } from '../../theme';
import { strings } from '../../i18n/strings';

interface OfflineBannerProps {
  onRetry: () => void;
}

/** Connection-lost banner shown between the tab strip and the active screen. */
export default function OfflineBanner({ onRetry }: OfflineBannerProps) {
  return (
    <Box
      role="alert"
      sx={{
        px: '10px', height: 26, flexShrink: 0, bgcolor: '#fff8e1',
        display: 'flex', alignItems: 'center', gap: 1,
      }}
    >
      <Typography sx={{ fontSize: 11, color: tokens.colorGoldDark, flex: 1 }}>
        {strings.connection.lost}
      </Typography>
      <Box
        component="button"
        onClick={onRetry}
        sx={{
          fontSize: 11, fontWeight: 600, px: 0.5, py: 0.25, borderRadius: '2px', cursor: 'pointer',
          border: 0, color: tokens.colorGoldDark,
          bgcolor: 'transparent', textTransform: 'none',
        }}
      >
        {strings.app.retry}
      </Box>
    </Box>
  );
}
