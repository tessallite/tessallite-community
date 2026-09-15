import { useState, useCallback, useRef, createContext, useContext, ReactNode } from 'react';
import { Button, Dialog, DialogActions, DialogContent, DialogContentText } from '@mui/material';
import { strings } from '../../i18n/strings';

/**
 * R3 (alert-mechanism audit, 2026-08-25): useExcel's confirmLargeResult had a
 * ConfirmGuard abstraction clearly meant to inject a styled dialog, but every
 * real caller (App.tsx, ReportBuilder.tsx) passed `undefined` for it, so it
 * always fell through to a native, unstyled window.confirm() -- the one
 * place in the task pane that looked nothing like the rest of the plugin.
 * This mirrors ToastProvider's exact pattern (Bug-6734: one policy, one
 * context, one rendered element) so every "are you sure?" in the plugin goes
 * through the same styled Dialog.
 */

type ConfirmFn = (message: string) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn>(() => Promise.resolve(true));

export function useConfirm(): ConfirmFn {
  return useContext(ConfirmContext);
}

interface PendingConfirm {
  message: string;
  resolve: (value: boolean) => void;
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  const pendingRef = useRef<PendingConfirm | null>(null);

  const confirm = useCallback<ConfirmFn>((message) => {
    return new Promise<boolean>((resolve) => {
      const entry: PendingConfirm = { message, resolve };
      pendingRef.current = entry;
      setPending(entry);
    });
  }, []);

  function settle(value: boolean) {
    pendingRef.current?.resolve(value);
    pendingRef.current = null;
    setPending(null);
  }

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <Dialog open={!!pending} onClose={() => settle(false)} maxWidth="xs" fullWidth>
        <DialogContent>
          <DialogContentText sx={{ fontSize: 13 }}>{pending?.message}</DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button size="small" onClick={() => settle(false)}>
            {strings.common.cancel}
          </Button>
          <Button size="small" variant="contained" onClick={() => settle(true)} autoFocus>
            {strings.common.continue}
          </Button>
        </DialogActions>
      </Dialog>
    </ConfirmContext.Provider>
  );
}
