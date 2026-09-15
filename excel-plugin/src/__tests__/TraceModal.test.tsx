import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import TraceModal from '../components/QueryTrace/TraceModal';
import { templates } from '../i18n/strings';
import type { SemanticQuery } from '../types/tessallite';

const query: SemanticQuery = { measures: ['m1'], dimensions: ['d1'] };

const MODEL_UUID = '5f1c2e3a-1111-4a2b-9c3d-000000000001';
const PERSONA_UUID = '5f1c2e3a-2222-4a2b-9c3d-000000000002';

describe('TraceModal', () => {
  // Bug-7396: the modal used to interpolate the raw model/persona UUIDs
  // directly into the header line. It must show the resolved display names
  // instead and never leak either raw id into the DOM.
  it('shows resolved model and persona display names, never the raw ids', () => {
    render(
      <TraceModal
        open
        onClose={vi.fn()}
        query={query}
        modelName="Sales Model"
        personaName="Finance Analyst"
      />,
    );
    expect(screen.getByText('Model: Sales Model | Persona: Finance Analyst')).toBeDefined();
    expect(screen.queryByText(MODEL_UUID, { exact: false })).toBeNull();
    expect(screen.queryByText(PERSONA_UUID, { exact: false })).toBeNull();
  });

  it('omits the persona segment when no persona name is available', () => {
    render(
      <TraceModal
        open
        onClose={vi.fn()}
        query={query}
        modelName="Sales Model"
        personaName={null}
      />,
    );
    expect(screen.getByText('Model: Sales Model')).toBeDefined();
  });

  it('omits the model segment rather than falling back to a raw id when the name cannot be resolved', () => {
    expect(templates.trace.modelPersona(null, 'Finance Analyst')).toBe('Persona: Finance Analyst');
    expect(templates.trace.modelPersona(null, null)).toBe('');
  });
});
