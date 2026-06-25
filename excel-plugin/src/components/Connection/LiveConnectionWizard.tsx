import { useState, useCallback } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions,
  Button, Typography, Box, Stepper, StepLabel, Step, ThemeProvider, TextField,
} from '@mui/material';
import { CheckCircle, ContentCopy } from '@mui/icons-material';
import { tokens, theme } from '../../theme';
import { buildMsolapConnectionString } from '../../utils/excelFormulas';

interface LiveConnectionWizardProps {
  open: boolean;
  onClose: () => void;
  serverUrl: string;
  catalog: string;
}

export default function LiveConnectionWizard({
  open, onClose, serverUrl, catalog,
}: LiveConnectionWizardProps) {
  const [step, setStep] = useState(0);
  const [xmlaUser, setXmlaUser] = useState('');

  const connectionString = buildMsolapConnectionString(
    serverUrl,
    catalog,
    xmlaUser || undefined,
  );

  const clearCredentials = useCallback(() => {
    setXmlaUser('');
  }, []);

  // F-025-18: Office.js exposes no `workbook.connections.add`, so automatic
  // creation never worked — the old code threw and silently fell through to
  // the manual step. Go straight to the manual setup instructions, the only
  // path that actually works.
  const handleCreateConnection = useCallback(() => {
    clearCredentials();
    setStep(1);
  }, [clearCredentials]);

  const handleClose = useCallback(() => {
    setStep(0);
    clearCredentials();
    onClose();
  }, [onClose, clearCredentials]);

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(connectionString).catch(() => {});
  }, [connectionString]);

  return (
    <ThemeProvider theme={theme}>
    <Dialog open={open} onClose={handleClose} maxWidth={false} sx={{ '& .MuiDialog-paper': { width: 360, borderRadius: 2 } }}>
      <DialogTitle sx={{ fontSize: 14, fontWeight: 700 }}>
        Live XMLA Connection
      </DialogTitle>

      <DialogContent sx={{ p: 2 }}>
        <Stepper activeStep={step} alternativeLabel sx={{ mb: 2 }}>
          <Step><StepLabel>Start</StepLabel></Step>
          <Step><StepLabel>Manual Setup</StepLabel></Step>
          <Step><StepLabel>Instructions</StepLabel></Step>
        </Stepper>

        {step === 0 && (
          <Box>
            <Typography sx={{ fontSize: 13, mb: 1 }}>
              A live XMLA connection lets Excel PivotTables query Tessallite directly.
              The add-in builds the connection string for you; you create the connection
              in Excel using that string, then use Excel's native PivotTable dialog.
            </Typography>
            <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, mb: 1 }}>
              Note: Excel does not let an add-in create connections or PivotTables
              programmatically, so these steps are done in Excel's own dialogs.
            </Typography>
            <TextField
              fullWidth
              size="small"
              label="XMLA Username (email)"
              value={xmlaUser}
              onChange={e => setXmlaUser(e.target.value)}
              sx={{ mb: 1 }}
            />
            <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary }}>
              Excel will prompt for your password when the connection is first used.
              Credentials are never stored in the workbook file.
            </Typography>
          </Box>
        )}

        {step === 1 && (
          <Box>
            <Typography sx={{ fontSize: 13, mb: 1 }}>
              Copy the connection string below and create the connection in Excel
              (Data &gt; Get Data &gt; From Other Sources &gt; From Analysis Services).
            </Typography>
            <Box sx={{ bgcolor: tokens.colorSubtleFill, p: 1, borderRadius: 1, mb: 1 }}>
              <Typography sx={{ fontSize: 11, fontFamily: tokens.fontMono, wordBreak: 'break-all' }}>
                {connectionString}
              </Typography>
            </Box>
            <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
              <Button
                size="small"
                variant="outlined"
                startIcon={<ContentCopy sx={{ fontSize: 14 }} />}
                onClick={handleCopy}
                sx={{ textTransform: 'none' }}
              >
                Copy
              </Button>
            </Box>
            <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary, mt: 1 }}>
              Paste this into Excel's Analysis Services connection dialog. Excel will prompt for your password when the connection is first used.
            </Typography>
          </Box>
        )}

        {step === 2 && (
          <Box>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1.5 }}>
              <CheckCircle sx={{ fontSize: 20, color: tokens.colorPrimary }} />
              <Typography sx={{ fontSize: 13, fontWeight: 600, color: tokens.colorPrimary }}>
                Connection created
              </Typography>
            </Box>
            <Typography sx={{ fontSize: 13, mb: 1 }}>
              To build a live PivotTable:
            </Typography>
            <Box component="ol" sx={{ fontSize: 12, color: tokens.colorCharcoal, pl: 2.5, m: 0, '& li': { mb: 0.5 } }}>
              <li>Go to <strong>Insert &gt; PivotTable</strong></li>
              <li>Select <strong>"Use an external data source"</strong></li>
              <li>Click <strong>"Choose Connection"</strong></li>
              <li>Select the <strong>"Tessallite"</strong> connection</li>
              <li>Choose where to place the PivotTable and click OK</li>
            </Box>
          </Box>
        )}
      </DialogContent>

      <DialogActions sx={{ px: 2, pb: 1.5 }}>
        <Button size="small" onClick={handleClose} sx={{ textTransform: 'none' }}>Close</Button>
        <Box sx={{ flex: 1 }} />
        {step === 0 && (
          <Button size="small" variant="contained" onClick={handleCreateConnection} sx={{ textTransform: 'none' }}>
            Create Connection
          </Button>
        )}
      </DialogActions>
    </Dialog>
    </ThemeProvider>
  );
}
