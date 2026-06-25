import { Box, Typography, Button } from '@mui/material';
import { InboxOutlined } from '@mui/icons-material';
import { tokens } from '../../theme';

interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void };
}

export default function EmptyState({
  icon,
  title,
  description,
  action,
}: EmptyStateProps) {
  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        py: 4,
        px: 2,
        gap: 1,
      }}
    >
      {icon || <InboxOutlined sx={{ fontSize: 48, color: tokens.colorBorder }} />}
      <Typography variant="h6" sx={{ fontSize: 14, fontWeight: 600, color: tokens.colorCharcoal, textAlign: 'center' }}>
        {title}
      </Typography>
      {description && (
        <Typography variant="body2" sx={{ fontSize: 12, color: tokens.colorTextSecondary, textAlign: 'center' }}>
          {description}
        </Typography>
      )}
      {action && (
        <Button
          variant="outlined"
          size="small"
          onClick={action.onClick}
          sx={{ mt: 1, textTransform: 'none' }}
        >
          {action.label}
        </Button>
      )}
    </Box>
  );
}
