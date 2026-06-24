import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import PersonaDropdown from '../components/PersonaSwitcher/PersonaDropdown';
import type { Persona } from '../types/tessallite';

const personas: Persona[] = [
  { id: 'p1', name: 'Executive', slug: 'exec', description: 'Exec view', audience: 'business', included_measure_ids: ['m1'], included_dimension_ids: [], included_hierarchy_ids: [] },
  { id: 'p2', name: 'Analyst', slug: 'analyst', description: 'Analyst view', audience: 'technical', included_measure_ids: ['m1', 'm2'], included_dimension_ids: [], included_hierarchy_ids: [] },
  { id: 'p3', name: 'Marketing', slug: 'mktg', description: 'Marketing view', included_measure_ids: [], included_dimension_ids: ['d1'], included_hierarchy_ids: [] },
];

describe('PersonaDropdown', () => {
  it('renders "Persona:" label', () => {
    render(<PersonaDropdown personas={personas} activePersonaId={null} onSelect={vi.fn()} />);
    expect(screen.getByText('Persona:')).toBeDefined();
  });

  it('shows "Default" as initially selected', () => {
    render(<PersonaDropdown personas={personas} activePersonaId={null} onSelect={vi.fn()} />);
    expect(screen.getByText('Default')).toBeDefined();
  });

  it('returns null when personas array is empty', () => {
    const { container } = render(<PersonaDropdown personas={[]} activePersonaId={null} onSelect={vi.fn()} />);
    expect(container.innerHTML).toBe('');
  });
});
