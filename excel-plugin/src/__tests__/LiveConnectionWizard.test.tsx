/**
 * Bug-6707 / Bug-6727 — the LiveConnectionWizard's connect step must:
 * 1. Instruct the user to set the connection's Friendly Name to exactly
 *    TESSALLITE_CONNECTION_NAME ("Tessallite") — every CUBE formula this
 *    add-in inserts hard-requires it (F-025-10).
 * 2. (Bug-6727) Use the Server-name flow ("From Database > From Analysis
 *    Services"), show the derived XMLA endpoint URL, and demote the raw
 *    MSOLAP connection string to an advanced expandable section.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import LiveConnectionWizard from '../components/Connection/LiveConnectionWizard';
import { TESSALLITE_CONNECTION_NAME } from '../utils/excelFormulas';

function openConnectStep() {
  render(
    <LiveConnectionWizard
      open
      onClose={vi.fn()}
      serverUrl="https://gateway.example.com"
      catalog="modelx"
    />,
  );
  // Step 0 requires an XMLA username before "Next" enables.
  fireEvent.change(screen.getByLabelText(/XMLA Username/i), {
    target: { value: 'analyst@example.com' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Next' }));
}

describe('LiveConnectionWizard connection-name instruction (Bug-6707)', () => {
  it('tells the user on the connect step to name the connection exactly TESSALLITE_CONNECTION_NAME', () => {
    openConnectStep();

    const instruction = screen.getByText(
      (content) => content.includes(`"${TESSALLITE_CONNECTION_NAME}"`) && /friendly name/i.test(content),
    );
    expect(instruction).toBeDefined();
    // The remedy for an already-created connection must also be present.
    expect(instruction.textContent).toMatch(/rename/i);
  });
});

describe('LiveConnectionWizard server-name flow (Bug-6727)', () => {
  it('shows the XMLA endpoint URL derived from serverUrl', () => {
    openConnectStep();

    // The XMLA endpoint URL should be visible on the connect step.
    expect(screen.getByText('https://gateway.example.com/api/v1/xmla/')).toBeDefined();
  });

  it('instructs the From Database > From Analysis Services menu path', () => {
    openConnectStep();

    expect(
      screen.getByText('Data > Get Data > From Database > From Analysis Services'),
    ).toBeDefined();
  });

  it('instructs checking Only Create Connection', () => {
    openConnectStep();

    expect(screen.getByText('"Only Create Connection"')).toBeDefined();
  });

  it('demotes the raw MSOLAP connection string to an advanced section', () => {
    openConnectStep();

    // The raw connection string should NOT be immediately visible.
    const advancedToggle = screen.getByText(/Advanced: raw connection string/i);
    expect(advancedToggle).toBeDefined();

    // Click to expand the advanced section.
    fireEvent.click(advancedToggle);

    // Now the MSOLAP string should be visible.
    expect(
      screen.getByText((content) => content.includes('Provider=MSOLAP')),
    ).toBeDefined();
  });
});
