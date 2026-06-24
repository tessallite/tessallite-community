import { useState, useCallback, createContext, useContext, ReactNode } from 'react';
import { Box, Typography, IconButton } from '@mui/material';
import { Close as CloseIcon, CheckCircleOutline, ErrorOutline, InfoOutlined, WarningAmberOutlined } from '@mui/icons-material';
import { tokens } from '../../theme';

type ToastSeverity = 'success' | 'error' | 'info' | 'warning';

interface Toast {
  id: string;
  message: string;
  severity: ToastSeverity;
}

interface ToastContextValue {
  showToast: (message: string, severity?: ToastSeverity) => void;
}

const ToastContext = createContext<ToastContextValue>({
  showToast: () => {},
});

export function useToast(): ToastContextValue {
  return useContext(ToastContext);
}

const severityStyles: Record<ToastSeverity, { bg: string; border: string; icon: ReactNode }> = {
  success: {
    bg: tokens.colorPrimaryBg,
    border: tokens.colorPrimary,
    icon: <CheckCircleOutline sx={{ fontSize: 14, color: tokens.colorPrimary }} />,
  },
  error: {
    bg: tokens.colorRedBg,
    border: tokens.colorRed,
    icon: <ErrorOutline sx={{ fontSize: 14, color: tokens.colorRed }} />,
  },
  info: {
    bg: tokens.colorSubtleFill,
    border: tokens.colorGoldDark,
    icon: <InfoOutlined sx={{ fontSize: 14, color: tokens.colorGoldDark }} />,
  },
  warning: {
    bg: tokens.colorGoldBg,
    border: '#ed6c02',
    icon: <WarningAmberOutlined sx={{ fontSize: 14, color: '#ed6c02' }} />,
  },
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const showToast = useCallback((message: string, severity: ToastSeverity = 'info') => {
    const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    setToasts(prev => [...prev, { id, message, severity }]);
    if (severity !== 'error') {
      setTimeout(() => {
        setToasts(prev => prev.filter(t => t.id !== id));
      }, 4000);
    }
  }, []);

  const dismissToast = useCallback((id: string) => {
    setToasts(prev => prev.filter(t => t.id !== id));
  }, []);

  return (
    <ToastContext.Provider value={{ showToast }}>
      {children}
      <Box
        sx={{
          position: 'fixed',
          bottom: 36,
          left: 8,
          right: 8,
          zIndex: 9999,
          display: 'flex',
          flexDirection: 'column',
          gap: 0.5,
        }}
      >
        {toasts.map(toast => {
          const style = severityStyles[toast.severity];
          return (
            <Box
              key={toast.id}
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 1,
                p: 0.75,
                pr: 0.5,
                borderRadius: 1,
                bgcolor: style.bg,
                borderLeft: `3px solid ${style.border}`,
                boxShadow: '0 2px 6px rgba(0,0,0,0.12)',
              }}
            >
              {style.icon}
              <Typography sx={{ flex: 1, fontSize: 11, color: tokens.colorCharcoal }}>
                {toast.message}
              </Typography>
              <IconButton size="small" onClick={() => dismissToast(toast.id)} sx={{ p: 0.25 }}>
                <CloseIcon sx={{ fontSize: 12, color: tokens.colorTextSecondary }} />
              </IconButton>
            </Box>
          );
        })}
      </Box>
    </ToastContext.Provider>
  );
}
