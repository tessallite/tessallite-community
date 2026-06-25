import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import LoginScreen from '../components/LoginScreen/LoginScreen';
import { ThemeProvider } from '@mui/material';
import { theme } from '../theme';

const mockStorage: Record<string, string> = {};
(globalThis as Record<string, unknown>).OfficeRuntime = {
  storage: {
    getItem: async (key: string) => mockStorage[key] ?? null,
    setItem: async (key: string, value: string) => { mockStorage[key] = value; },
    removeItem: async (key: string) => { delete mockStorage[key]; },
  },
};

function renderLogin(props: Partial<Parameters<typeof LoginScreen>[0]> = {}) {
  return render(
    <ThemeProvider theme={theme}>
      <LoginScreen
        onLogin={props.onLogin || vi.fn()}
        loading={props.loading || false}
        error={props.error || null}
      />
    </ThemeProvider>,
  );
}

describe('LoginScreen', () => {
  it('renders server URL, tenant, email, and password fields', async () => {
    renderLogin();
    await waitFor(() => {
      expect(screen.getByLabelText('Server URL')).toBeDefined();
      expect(screen.getByLabelText('Tenant')).toBeDefined();
      expect(screen.getByLabelText('Email')).toBeDefined();
      expect(screen.getByLabelText('Password')).toBeDefined();
    });
  });

  it('calls onLogin with form values on submit', async () => {
    const onLogin = vi.fn();
    renderLogin({ onLogin });
    await waitFor(() => {
      expect(screen.getByLabelText('Server URL')).toBeDefined();
    });
    fireEvent.change(screen.getByLabelText('Server URL'), { target: { value: 'https://test.com' } });
    fireEvent.change(screen.getByLabelText('Tenant'), { target: { value: 'test-tenant' } });
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'user@test.com' } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'secret' } });
    fireEvent.submit(screen.getByLabelText('Server URL').closest('form')!);
    await waitFor(() => {
      expect(onLogin).toHaveBeenCalledWith(
        { serverUrl: 'https://test.com', tenantId: 'test-tenant', email: 'user@test.com', password: 'secret' },
        false,
      );
    });
  });

  it('shows error alert when error prop is set', async () => {
    renderLogin({ error: 'Invalid credentials' });
    await waitFor(() => {
      expect(screen.getByText('Invalid credentials')).toBeDefined();
    });
  });

  it('submit button shows CircularProgress when loading', async () => {
    renderLogin({ loading: true });
    await waitFor(() => {
      expect(screen.queryByText('Sign In')).toBeNull();
      const btns = screen.getAllByRole('button');
      const submitBtn = btns.find(b => b.getAttribute('type') === 'submit');
      expect(submitBtn).toBeDefined();
      expect(submitBtn!.hasAttribute('disabled')).toBe(true);
    });
  });

  it('submit button is disabled when form is incomplete', async () => {
    renderLogin();
    await waitFor(() => {
      const btns = screen.getAllByRole('button');
      const submitBtn = btns.find(b => b.getAttribute('type') === 'submit');
      expect(submitBtn).toBeDefined();
      expect(submitBtn!.hasAttribute('disabled')).toBe(true);
    });
  });
});
