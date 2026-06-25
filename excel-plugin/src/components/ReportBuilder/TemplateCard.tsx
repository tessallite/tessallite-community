import { Box, Typography } from '@mui/material';
import { Timeline, Leaderboard, CompareArrows, Public, ShowChart, Assessment } from '@mui/icons-material';
import { tokens } from '../../theme';
import type { ReportTemplate } from '../../utils/reportTemplates';

interface TemplateCardProps {
  template: ReportTemplate;
  isApplicable: boolean;
  missingFields: string[];
  onSelect: (template: ReportTemplate) => void;
}

const iconMap: Record<string, React.ElementType> = {
  Timeline,
  Leaderboard,
  CompareArrows,
  Public,
  ShowChart,
  Assessment,
};

export default function TemplateCard({
  template,
  isApplicable,
  missingFields,
  onSelect,
}: TemplateCardProps) {
  const IconComp = iconMap[template.icon] || Assessment;

  return (
    <Box
      onClick={() => isApplicable && onSelect(template)}
      sx={{
        width: 140,
        p: 1,
        border: `1px solid ${tokens.colorBorderLight}`,
        borderRadius: 1,
        cursor: isApplicable ? 'pointer' : 'not-allowed',
        opacity: isApplicable ? 1 : 0.5,
        '&:hover': isApplicable ? { borderColor: tokens.colorPrimary, bgcolor: tokens.colorPrimaryBg } : {},
      }}
    >
      <IconComp sx={{ fontSize: 24, color: tokens.colorPrimary, mb: 0.5 }} />
      <Typography sx={{ fontSize: 13, fontWeight: 600, mb: 0.25 }}>
        {template.name}
      </Typography>
      <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, lineHeight: 1.3 }}>
        {template.description}
      </Typography>
      {!isApplicable && (
        <Typography sx={{ fontSize: 10, color: tokens.colorRed, mt: 0.5 }}>
          Missing: {missingFields.join(', ')}
        </Typography>
      )}
    </Box>
  );
}
