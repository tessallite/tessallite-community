import { useState, useCallback, useMemo } from 'react';
import {
  Dialog, DialogTitle, DialogContent, DialogActions,
  Button, Typography, Box, Stepper, StepLabel, Step, ThemeProvider, TextField,
  Collapse,
} from '@mui/material';
import { CheckCircle, ContentCopy, ExpandMore, ExpandLess } from '@mui/icons-material';
import { tokens, theme } from '../../theme';
import { buildMsolapConnectionString, TESSALLITE_CONNECTION_NAME } from '../../utils/excelFormulas';
import { strings, templates } from '../../i18n/strings';

interface LiveConnectionWizardProps {
  open: boolean;
  onClose: () => void;
  serverUrl: string;
  catalog: string;
}

// eslint-disable-next-line no-control-regex
const CONTROL_CHAR_RE = /[\x00-\x1f\x7f]/;

function validateXmlaUser(value: string): string | null {
  if (!value.trim()) return strings.liveConnection.xmlaUsernameRequired;
  if (CONTROL_CHAR_RE.test(value)) return strings.liveConnection.xmlaUsernameInvalid;
  return null;
}

export default function LiveConnectionWizard({
  open, onClose, serverUrl, catalog,
}: LiveConnectionWizardProps) {
  const [step, setStep] = useState(0);
  const [xmlaUser, setXmlaUser] = useState('');
  const [touched, setTouched] = useState(false);
  // Bug-6727: the raw MSOLAP connection string is demoted to an advanced
  // expandable section so the primary flow uses the simpler Server-name path.
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const xmlaUserError = touched ? validateXmlaUser(xmlaUser) : null;

  // Bug-6727: derive the XMLA endpoint URL from the gateway serverUrl.
  // This is the value the user pastes into the "Server name" field.
  const xmlaEndpointUrl = useMemo(() => {
    const base = serverUrl.replace(/\/$/, '');
    return `${base}/api/v1/xmla/`;
  }, [serverUrl]);

  const connectionString = buildMsolapConnectionString(
    serverUrl,
    catalog,
    xmlaUser || undefined,
  );

  const clearCredentials = useCallback(() => {
    setXmlaUser('');
    setTouched(false);
  }, []);

  // F-025-18: Office.js exposes no `workbook.connections.add`, so automatic
  // creation never worked — the old code threw and silently fell through to
  // the manual step. Go straight to the manual setup instructions, the only
  // path that actually works.
  // Bug-5784: do NOT clear credentials here — the user's xmlaUser input from
  // step 0 is needed to build the connection string shown in step 1.
  const handleCreateConnection = useCallback(() => {
    setStep(1);
  }, []);

  const handleClose = useCallback(() => {
    setStep(0);
    setAdvancedOpen(false);
    clearCredentials();
    onClose();
  }, [onClose, clearCredentials]);

  const handleCopyServerUrl = useCallback(() => {
    navigator.clipboard.writeText(xmlaEndpointUrl).catch(() => {});
  }, [xmlaEndpointUrl]);

  const handleCopyConnectionString = useCallback(() => {
    navigator.clipboard.writeText(connectionString).catch(() => {});
  }, [connectionString]);

  return (
    <ThemeProvider theme={theme}>
    <Dialog open={open} onClose={handleClose} maxWidth={false} sx={{ '& .MuiDialog-paper': { width: 380, borderRadius: 2 } }}>
      <DialogTitle sx={{ fontSize: 14, fontWeight: 700 }}>
        {strings.liveConnection.title}
      </DialogTitle>

      <DialogContent sx={{ p: 2 }}>
        <Stepper activeStep={step} alternativeLabel sx={{ mb: 2 }}>
          <Step><StepLabel>{strings.liveConnection.stepStart}</StepLabel></Step>
          <Step><StepLabel>{strings.liveConnection.stepConnect}</StepLabel></Step>
          <Step><StepLabel>{strings.liveConnection.stepInstructions}</StepLabel></Step>
        </Stepper>

        {step === 0 && (
          <Box>
            <Typography sx={{ fontSize: 13, mb: 1 }}>
              {strings.liveConnection.description}
            </Typography>
            <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, mb: 1 }}>
              {strings.liveConnection.note}
            </Typography>
            <TextField
              fullWidth
              size="small"
              label={strings.liveConnection.xmlaUsernameLabel}
              value={xmlaUser}
              onChange={e => setXmlaUser(e.target.value)}
              onBlur={() => setTouched(true)}
              error={!!xmlaUserError}
              helperText={xmlaUserError}
              sx={{ mb: 1 }}
            />
            <Typography sx={{ fontSize: 10, color: tokens.colorTextSecondary }}>
              {strings.liveConnection.credentialNote}
            </Typography>
          </Box>
        )}

        {step === 1 && (
          <Box>
            {/* Bug-6727: Server-name flow — the primary path that works in
                current Excel (desktop and web). The old "From Other Sources >
                Analysis Services" menu path no longer exists in the modern
                ribbon and the raw MSOLAP string cannot be pasted into the
                modern connection dialog. */}
            <Typography sx={{ fontSize: 13, fontWeight: 600, mb: 0.75 }}>
              {strings.liveConnection.serverFlowTitle}
            </Typography>
            <Box component="ol" sx={{ fontSize: 12, color: tokens.colorCharcoal, pl: 2.5, m: 0, mb: 1, '& li': { mb: 0.5 } }}>
              <li>{strings.liveConnection.serverStep1Prefix}<strong>{strings.liveConnection.serverStep1Bold}</strong></li>
              <li>{strings.liveConnection.serverStep2}</li>
              <li>
                {strings.liveConnection.serverStep3Prefix}
                <Box component="span" sx={{ fontFamily: tokens.fontMono, fontSize: 11, bgcolor: tokens.colorSubtleFill, px: 0.5, borderRadius: 0.5 }}>
                  {xmlaEndpointUrl}
                </Box>
                <Button
                  size="small"
                  variant="text"
                  startIcon={<ContentCopy sx={{ fontSize: 12 }} />}
                  onClick={handleCopyServerUrl}
                  sx={{ textTransform: 'none', fontSize: 10, ml: 0.5, minWidth: 'auto', py: 0 }}
                >
                  {strings.liveConnection.copy}
                </Button>
              </li>
              <li>{strings.liveConnection.serverStep4Prefix}<strong>{strings.liveConnection.serverStep4Bold}</strong></li>
              <li>{strings.liveConnection.serverStep5}</li>
            </Box>

            {/* Bug-6707: the friendly name is the binding contract for every
                CUBE formula this add-in inserts (F-025-10). Excel defaults it
                from the server/catalog, so without this step the user creates
                a connection the formulas can never resolve against. */}
            <Typography sx={{ fontSize: 11, fontWeight: 600, color: tokens.colorGoldDark, mb: 1 }}>
              {templates.liveConnection.connectionNameInstruction(TESSALLITE_CONNECTION_NAME)}
            </Typography>

            {/* Bug-6727: raw connection string demoted to expandable advanced note */}
            <Box sx={{ borderTop: `1px solid ${tokens.colorBorderLight}`, pt: 0.75 }}>
              <Box
                component="button"
                onClick={() => setAdvancedOpen(!advancedOpen)}
                sx={{
                  display: 'flex', alignItems: 'center', gap: 0.5,
                  fontSize: 11, fontWeight: 600, color: tokens.colorTextSecondary,
                  bgcolor: 'transparent', border: 'none', cursor: 'pointer', p: 0,
                }}
              >
                {advancedOpen ? <ExpandLess sx={{ fontSize: 16 }} /> : <ExpandMore sx={{ fontSize: 16 }} />}
                {strings.liveConnection.advancedToggle}
              </Box>
              <Collapse in={advancedOpen}>
                <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary, mt: 0.5, mb: 0.5 }}>
                  {strings.liveConnection.advancedNote}
                </Typography>
                <Box sx={{ bgcolor: tokens.colorSubtleFill, p: 1, borderRadius: 1, mb: 0.5 }}>
                  <Typography sx={{ fontSize: 10, fontFamily: tokens.fontMono, wordBreak: 'break-all' }}>
                    {connectionString}
                  </Typography>
                </Box>
                <Button
                  size="small"
                  variant="outlined"
                  startIcon={<ContentCopy sx={{ fontSize: 12 }} />}
                  onClick={handleCopyConnectionString}
                  sx={{ textTransform: 'none', fontSize: 10 }}
                >
                  {strings.liveConnection.copyConnectionString}
                </Button>
              </Collapse>
            </Box>
          </Box>
        )}

        {step === 2 && (
          <Box>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1.5 }}>
              <CheckCircle sx={{ fontSize: 20, color: tokens.colorPrimary }} />
              <Typography sx={{ fontSize: 13, fontWeight: 600, color: tokens.colorPrimary }}>
                {strings.liveConnection.connectionCreated}
              </Typography>
            </Box>
            <Typography sx={{ fontSize: 13, mb: 1 }}>
              {strings.liveConnection.pivotInstructions}
            </Typography>
            <Box component="ol" sx={{ fontSize: 12, color: tokens.colorCharcoal, pl: 2.5, m: 0, '& li': { mb: 0.5 } }}>
              <li>{strings.liveConnection.pivotStep1Prefix}<strong>{strings.liveConnection.pivotStep1Bold}</strong></li>
              <li>{strings.liveConnection.pivotStep2Prefix}<strong>{strings.liveConnection.pivotStep2Bold}</strong></li>
              <li>{strings.liveConnection.pivotStep3Prefix}<strong>{strings.liveConnection.pivotStep3Bold}</strong></li>
              <li>{strings.liveConnection.pivotStep4Prefix}<strong>{strings.liveConnection.pivotStep4Bold}</strong>{strings.liveConnection.pivotStep4Suffix}</li>
              <li>{strings.liveConnection.pivotStep5}</li>
            </Box>
          </Box>
        )}
      </DialogContent>

      <DialogActions sx={{ px: 2, pb: 1.5 }}>
        <Button size="small" onClick={handleClose} sx={{ textTransform: 'none' }}>{strings.liveConnection.close}</Button>
        <Box sx={{ flex: 1 }} />
        {step > 0 && (
          <Button size="small" onClick={() => setStep(step - 1)} sx={{ textTransform: 'none' }}>
            {strings.liveConnection.back}
          </Button>
        )}
        {step === 0 && (
          <Button
            size="small"
            variant="contained"
            disabled={!!validateXmlaUser(xmlaUser)}
            onClick={() => { setTouched(true); if (!validateXmlaUser(xmlaUser)) handleCreateConnection(); }}
            sx={{ textTransform: 'none' }}
          >
            {strings.liveConnection.next}
          </Button>
        )}
        {step === 1 && (
          <Button size="small" variant="contained" onClick={() => setStep(2)} sx={{ textTransform: 'none' }}>
            {strings.liveConnection.next}
          </Button>
        )}
      </DialogActions>
    </Dialog>
    </ThemeProvider>
  );
}
