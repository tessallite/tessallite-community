import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { useState } from 'react';
import { ConfirmProvider, useConfirm } from './ConfirmProvider';

// R3 (alert-mechanism audit, 2026-08-25): the task pane's confirmLargeResult
// had a ConfirmGuard abstraction that no real caller ever wired up, so it
// always fell through to a native window.confirm() -- a jarring, unstyled
// dialog next to everything else in the plugin. This provider is the styled
// replacement; these tests exercise the real dialog, not a mock.

function Harness() {
  const confirm = useConfirm();
  const [result, setResult] = useState<string>('pending');

  return (
    <div>
      <button
        onClick={async () => {
          const ok = await confirm('This result contains 15,000 rows. Continue?');
          setResult(ok ? 'confirmed' : 'cancelled');
        }}
      >
        Ask
      </button>
      <span data-testid="result">{result}</span>
    </div>
  );
}

function renderHarness() {
  return render(
    <ConfirmProvider>
      <Harness />
    </ConfirmProvider>,
  );
}

describe('ConfirmProvider', () => {
  it('shows the exact message passed to confirm()', async () => {
    renderHarness();
    fireEvent.click(screen.getByText('Ask'));
    expect(await screen.findByText('This result contains 15,000 rows. Continue?')).toBeTruthy();
  });

  it('resolves true when Continue is clicked', async () => {
    renderHarness();
    fireEvent.click(screen.getByText('Ask'));
    fireEvent.click(await screen.findByText('Continue'));
    expect(await screen.findByText('confirmed')).toBeTruthy();
  });

  it('resolves false when Cancel is clicked', async () => {
    renderHarness();
    fireEvent.click(screen.getByText('Ask'));
    fireEvent.click(await screen.findByText('Cancel'));
    expect(await screen.findByText('cancelled')).toBeTruthy();
  });

  it('never invokes the native window.confirm', async () => {
    const nativeConfirm = vi.spyOn(window, 'confirm');
    renderHarness();
    fireEvent.click(screen.getByText('Ask'));
    fireEvent.click(await screen.findByText('Continue'));
    expect(await screen.findByText('confirmed')).toBeTruthy();
    expect(nativeConfirm).not.toHaveBeenCalled();
    nativeConfirm.mockRestore();
  });

  it('defaults to always-resolve-true outside a provider (safe no-op)', () => {
    // useConfirm()'s context default must never leave a caller hanging if a
    // component somehow renders outside ConfirmProvider.
    let resolvedValue: boolean | undefined;
    function Standalone() {
      const confirm = useConfirm();
      confirm('unused').then((v) => {
        resolvedValue = v;
      });
      return null;
    }
    render(<Standalone />);
    return new Promise<void>((resolve) => {
      setTimeout(() => {
        expect(resolvedValue).toBe(true);
        resolve();
      }, 0);
    });
  });
});
