/**
 * F-025-02 — CUBE Formula Wizard validation.
 *
 * The previous wizard POSTed the generated `=CUBEVALUE(...)` formula text to
 * /validate, which parses raw_query as SQL/DAX — an Excel formula is never SQL,
 * so validation ALWAYS failed and the Insert button stayed permanently
 * disabled. The fix validates the semantic intent client-side (the measure and
 * the chosen member are already resolved from the model API). These tests
 * assert that a valid selection reaches "Ready to insert" and enables Insert,
 * and that the wizard no longer calls the SQL validate endpoint at all.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import CubeFormulaWizard from '../components/CubeFunctions/CubeFormulaWizard';
import type { FieldCompatibilityResponse } from '../types/tessallite';

// Spy on the query-router client to prove validate is never called.
const validateSpy = vi.fn();
const mockState = vi.hoisted(() => ({
  compatibility: undefined as FieldCompatibilityResponse | undefined,
}));
vi.mock('../api/queryRouter', () => ({
  discoverMembers: vi.fn(async () => ({
    members: [{ name: 'CREDIT', key: 'CREDIT' }, { name: 'LOAN', key: 'LOAN' }],
  })),
  validateQuery: (...args: unknown[]) => { validateSpy(...args); return Promise.resolve({ ok: true }); },
}));

vi.mock('../hooks/useModel', () => ({
  useFieldCompatibility: () => ({
    data: mockState.compatibility,
    isLoading: false,
    isError: false,
  }),
}));

// F-025-18: the hook no longer detects connections (Office.js has no
// workbook.connections API); it exposes connectionStatus 'unknown' and a no-op
// refresh. Stub it so the wizard renders without an Excel host.
vi.mock('../hooks/useExcelConnections', () => ({
  useExcelConnections: () => ({
    connectionStatus: 'unknown',
    refreshConnections: vi.fn(),
  }),
}));

const MEASURES = [
  { id: 'meas-1', name: 'base_amount', display_name: 'Base Amount', default_agg: 'sum' },
] as never[];
const DIMENSIONS = [
  { id: 'dim-1', name: 'account_type', display_name: 'Account Type' },
  { id: 'dim-2', name: 'product_category', display_name: 'Product Category' },
] as never[];

function renderWizard(onInsertFormula = vi.fn(), dimensions = DIMENSIONS) {
  render(
    <CubeFormulaWizard
      open
      onClose={vi.fn()}
      measures={MEASURES}
      dimensions={dimensions}
      projectId="p1"
      modelId="m1"
      connectionName="Tessallite"
      onInsertFormula={onInsertFormula}
    />,
  );
}

beforeEach(() => {
  validateSpy.mockClear();
  mockState.compatibility = {
    model_id: 'm1',
    version_id: 'v1',
    generated_at: '2026-06-14T00:00:00Z',
    status: 'compatible',
    measures: {
      'meas-1': {
        name: 'Base Amount',
        compatible_dimension_ids: ['dim-1', 'dim-2'],
        incompatible_dimensions: {},
      },
    },
    multi_measure: null,
  };
});

describe('CubeFormulaWizard (F-025-02)', () => {
  it('enables Insert for a valid measure-only selection and never calls validate', async () => {
    renderWizard();

    // Step 1: pick a measure.
    fireEvent.mouseDown(screen.getByRole('combobox'));
    fireEvent.click(await screen.findByText('Base Amount'));
    fireEvent.click(screen.getByRole('button', { name: /next/i }));

    // Step 2: skip the optional filter.
    fireEvent.click(await screen.findByRole('button', { name: /next/i }));

    // Step 3: validation runs client-side -> Ready to insert, Insert enabled.
    await waitFor(() => {
      expect(screen.getByText(/Ready to insert/i)).toBeDefined();
    });
    const insertBtn = screen.getByRole('button', { name: /insert formula/i });
    expect(insertBtn.hasAttribute('disabled')).toBe(false);

    // The formula-text SQL validate endpoint must NOT be called.
    expect(validateSpy).not.toHaveBeenCalled();
  });

  it('inserts a CUBEVALUE formula using the TECHNICAL measure name, not the display name', async () => {
    const onInsert = vi.fn();
    renderWizard(onInsert);

    fireEvent.mouseDown(screen.getByRole('combobox'));
    fireEvent.click(await screen.findByText('Base Amount'));
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    fireEvent.click(await screen.findByRole('button', { name: /next/i }));
    await waitFor(() => screen.getByText(/Ready to insert/i));

    fireEvent.click(screen.getByRole('button', { name: /insert formula/i }));
    expect(onInsert).toHaveBeenCalledTimes(1);
    const [formula] = onInsert.mock.calls[0];
    expect(formula).toContain('CUBEVALUE');
    // M-1: the gateway resolves [Measures].[<technical name>] only. The emitted
    // formula must carry the technical name `base_amount`, never the display
    // name "Base Amount" (which the gateway returns empty -> #N/A on refresh).
    expect(formula).toContain('[Measures].[base_amount]');
    expect(formula).not.toContain('Base Amount');
  });

  it('emits the dimension+member reference with TECHNICAL dimension name and member key', async () => {
    const onInsert = vi.fn();
    renderWizard(onInsert);

    // Step 1: measure.
    fireEvent.mouseDown(screen.getByRole('combobox'));
    fireEvent.click(await screen.findByText('Base Amount'));
    fireEvent.click(screen.getByRole('button', { name: /next/i }));

    // Step 2: choose the dimension filter (the only combobox), then the member
    // (the second combobox that appears once the dimension's members load).
    fireEvent.mouseDown(screen.getByRole('combobox'));
    fireEvent.click(await screen.findByText('Account Type'));
    // Member select renders after discoverMembers resolves -> 2 comboboxes.
    await waitFor(() => expect(screen.getAllByRole('combobox').length).toBe(2));
    const memberCombo = screen.getAllByRole('combobox')[1];
    fireEvent.mouseDown(memberCombo);
    fireEvent.click(await screen.findByRole('option', { name: 'CREDIT' }));
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    await waitFor(() => screen.getByText(/Ready to insert/i));

    fireEvent.click(screen.getByRole('button', { name: /insert formula/i }));
    const [formula] = onInsert.mock.calls[0];
    // Technical dimension name `account_type` (NOT "Account Type") and the
    // member key, in the [Dim].[Dim].[Member] form the gateway accepts.
    expect(formula).toContain('[account_type].[account_type].[CREDIT]');
    expect(formula).not.toContain('Account Type');
  });

  it('filters optional dimension filters to dimensions compatible with the selected measure', async () => {
    mockState.compatibility = {
      model_id: 'm1',
      version_id: 'v1',
      generated_at: '2026-06-14T00:00:00Z',
      status: 'incompatible',
      measures: {
        'meas-1': {
          name: 'Base Amount',
          compatible_dimension_ids: ['dim-1'],
          incompatible_dimensions: {
            'dim-2': {
              code: 'NO_JOIN_PATH',
              message: 'Product Category cannot be used with Base Amount.',
              measure_id: 'meas-1',
              dimension_id: 'dim-2',
              compatible_dimension_ids: ['dim-1'],
              compatible_dimension_names: ['Account Type'],
            },
          },
        },
      },
      multi_measure: null,
    };
    renderWizard();

    fireEvent.mouseDown(screen.getByRole('combobox'));
    fireEvent.click(await screen.findByText('Base Amount'));
    fireEvent.click(screen.getByRole('button', { name: /next/i }));

    fireEvent.mouseDown(screen.getByRole('combobox'));
    expect(await screen.findByRole('option', { name: 'Account Type' })).toBeDefined();
    expect(screen.queryByRole('option', { name: 'Product Category' })).toBeNull();
  });
});
