import { useState, useCallback, useEffect, useMemo, FormEvent } from 'react';
import {
  Box, TextField, Button, Typography, Checkbox, FormControlLabel,
  Alert, InputAdornment, IconButton, CircularProgress,
} from '@mui/material';
import { Visibility, VisibilityOff } from '@mui/icons-material';
import type { LoginFormData } from '../../hooks/useAuth';
import type { ConnectionProfile } from '../../utils/storage';
import { getProfiles } from '../../utils/storage';
import { tokens } from '../../theme';
import { strings } from '../../i18n/strings';

interface LoginScreenProps {
  onLogin: (data: LoginFormData, remember: boolean) => Promise<void>;
  loading: boolean;
  error: string | null;
}

function TessalliteLogo() {
  return (
    <svg width="32" height="28" viewBox="0 0 32 28" fill="none" aria-hidden="true">
      <polygon points="0,7 8,0 16,7 8,14" fill="#185a33" />
      <polygon points="16,7 24,0 32,7 24,14" fill="#c9a520" />
      <polygon points="0,21 8,14 16,21 8,28" fill="#217346" />
      <polygon points="16,21 24,14 32,21 24,28" fill="#185a33" />
    </svg>
  );
}

export default function LoginScreen({ onLogin, loading, error }: LoginScreenProps) {
  const [serverUrl, setServerUrl] = useState(() => {
    const origin = window.location.origin;
    return origin && origin !== 'null' ? origin : '';
  });
  const [tenantId, setTenantId] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [remember, setRemember] = useState(false);
  const [savedProfiles, setSavedProfiles] = useState<ConnectionProfile[]>([]);

  useEffect(() => {
    getProfiles().then(setSavedProfiles);
  }, []);

  useEffect(() => {
    if (savedProfiles.length > 0 && !serverUrl) {
      const p = savedProfiles[0];
      setServerUrl(p.serverUrl);
      setTenantId(p.tenantId);
      setEmail(p.email);
    }
  }, [savedProfiles, serverUrl]);

  const handleSubmit = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    await onLogin({ serverUrl, tenantId, email, password }, remember);
    setPassword('');
  }, [serverUrl, tenantId, email, password, remember, onLogin]);

  // F-41: Validate server URL format
  const serverUrlError = useMemo(() => {
    if (!serverUrl) return '';
    if (!/^https?:\/\//i.test(serverUrl)) return strings.login.serverUrlMustStart;
    try { new URL(serverUrl); return ''; } catch { return strings.login.serverUrlInvalid; }
  }, [serverUrl]);

  const isValid = serverUrl && !serverUrlError && tenantId && email && password;

  return (
    <Box
      component="form"
      onSubmit={handleSubmit}
      aria-label={strings.login.formAria}
      sx={{
        display: 'flex',
        flexDirection: 'column',
        px: 2.5,
        pt: 3,
        pb: 4,
        height: '100%',
        gap: 1.5,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.5 }}>
        <TessalliteLogo />
        <Box>
          <Typography sx={{ fontSize: 14, fontWeight: 600, color: tokens.colorCharcoal, lineHeight: 1.2 }}>
            {strings.login.brandName}
          </Typography>
          <Typography sx={{ fontSize: 11, color: tokens.colorTextSecondary }}>
            {strings.login.tagline}
          </Typography>
        </Box>
      </Box>

      <TextField
        label={strings.login.serverUrl}
        placeholder={strings.login.serverUrlPlaceholder}
        value={serverUrl}
        onChange={e => setServerUrl(e.target.value)}
        error={!!serverUrlError}
        helperText={serverUrlError}
        fullWidth
        size="small"
      />
      <TextField
        label={strings.login.tenant}
        placeholder={strings.login.tenantPlaceholder}
        value={tenantId}
        onChange={e => setTenantId(e.target.value)}
        fullWidth
        size="small"
      />
      <TextField
        label={strings.login.email}
        placeholder={strings.login.emailPlaceholder}
        type="email"
        value={email}
        onChange={e => setEmail(e.target.value)}
        fullWidth
        size="small"
      />
      <TextField
        label={strings.login.password}
        placeholder={strings.login.passwordPlaceholder}
        type={showPassword ? 'text' : 'password'}
        value={password}
        onChange={e => setPassword(e.target.value)}
        fullWidth
        size="small"
        InputProps={{
          endAdornment: (
            <InputAdornment position="end">
              <IconButton size="small" onClick={() => setShowPassword(!showPassword)} aria-label={showPassword ? strings.login.hidePassword : strings.login.showPassword}>
                {showPassword ? <VisibilityOff fontSize="small" /> : <Visibility fontSize="small" />}
              </IconButton>
            </InputAdornment>
          ),
        }}
      />

      <FormControlLabel
        control={
          <Checkbox
            size="small"
            checked={remember}
            onChange={e => setRemember(e.target.checked)}
          />
        }
        label={<Typography sx={{ fontSize: 12, color: tokens.colorTextSecondary }}>{strings.login.rememberProfile}</Typography>}
        sx={{ mr: 0, '.MuiFormControlLabel-label': { fontSize: 12 } }}
      />

      {error && (
        <Alert severity="error" variant="outlined" sx={{ fontSize: 11, py: 0 }}>
          {error}
        </Alert>
      )}

      <Button
        type="submit"
        variant="contained"
        fullWidth
        disabled={!isValid || loading}
        sx={{ mt: 0.5 }}
      >
        {loading ? <CircularProgress size={18} sx={{ color: 'white' }} /> : strings.login.connect}
      </Button>
    </Box>
  );
}
