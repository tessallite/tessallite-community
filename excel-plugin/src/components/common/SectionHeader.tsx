import { Box, Typography, IconButton } from '@mui/material';
import { ExpandMoreOutlined } from '@mui/icons-material';
import { tokens } from '../../theme';

interface SectionHeaderProps {
  title: string;
  count?: number;
  collapsed: boolean;
  onToggle: () => void;
}

export default function SectionHeader({
  title,
  count,
  collapsed,
  onToggle,
}: SectionHeaderProps) {
  return (
    <Box
      onClick={onToggle}
      sx={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        px: 1.5,
        py: 0.5,
        cursor: 'pointer',
        borderTop: `1px solid ${tokens.colorBorderLight}`,
        '&:hover': { bgcolor: tokens.colorSubtleFill },
      }}
      role="button"
      aria-expanded={!collapsed}
      tabIndex={0}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggle(); } }}
    >
      <Typography sx={{ fontSize: 13, fontWeight: 700, color: tokens.colorCharcoal, textTransform: 'uppercase' }}>
        {title}{count !== undefined ? ` (${count})` : ''}
      </Typography>
      <IconButton size="small" onClick={e => { e.stopPropagation(); onToggle(); }}>
        <ExpandMoreOutlined
          sx={{
            fontSize: 16,
            color: tokens.colorTextSecondary,
            transform: collapsed ? 'rotate(0deg)' : 'rotate(180deg)',
            transition: 'transform 0.2s',
          }}
        />
      </IconButton>
    </Box>
  );
}
