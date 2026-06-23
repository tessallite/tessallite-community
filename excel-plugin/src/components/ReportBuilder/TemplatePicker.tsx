import { Box, Typography, Dialog, DialogTitle, DialogContent, IconButton, ThemeProvider } from '@mui/material';
import { Close } from '@mui/icons-material';
import { tokens, theme } from '../../theme';
import { REPORT_TEMPLATES, ReportTemplate } from '../../utils/reportTemplates';
import TemplateCard from './TemplateCard';

interface TemplatePickerProps {
  open: boolean;
  onClose: () => void;
  onSelect: (template: ReportTemplate) => void;
  measureCount: number;
  hasTimeDimension: boolean;
  hasCategoricalDimension: boolean;
}

export default function TemplatePicker({
  open,
  onClose,
  onSelect,
  measureCount,
  hasTimeDimension,
  hasCategoricalDimension,
}: TemplatePickerProps) {
  return (
    <ThemeProvider theme={theme}>
    <Dialog open={open} onClose={onClose} maxWidth={false} sx={{ '& .MuiDialog-paper': { width: 320, borderRadius: 2 } }}>
      <DialogTitle sx={{ fontSize: 14, fontWeight: 700, pb: 0, display: 'flex', alignItems: 'center' }}>
        Report Templates
        <IconButton size="small" onClick={onClose} sx={{ ml: 'auto' }}>
          <Close fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent sx={{ p: 1.5 }}>
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
          {REPORT_TEMPLATES.map(template => {
            const missing: string[] = [];
            if (template.requiresMeasure && measureCount === 0) missing.push('measure');
            if (template.requiresTimeDimension && !hasTimeDimension) missing.push('time dim');
            if (template.requiresCategoricalDimension && !hasCategoricalDimension) missing.push('category dim');
            if (template.requiresComparisonMeasure && measureCount < 2) missing.push('2nd measure');
            const valid = missing.length === 0;

            return (
              <TemplateCard
                key={template.id}
                template={template}
                isApplicable={valid}
                missingFields={missing}
                onSelect={onSelect}
              />
            );
          })}
        </Box>
      </DialogContent>
    </Dialog>
    </ThemeProvider>
  );
}
