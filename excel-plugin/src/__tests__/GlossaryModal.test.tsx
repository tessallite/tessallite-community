import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import GlossaryModal from '../components/Glossary/GlossaryModal';
import type { GlossaryEntry } from '../types/tessallite';

const mockEntries: GlossaryEntry[] = [
  { id: '1', term: 'Revenue', definition: 'Total income from sales', context_notes: 'Finance', source: 'user', status: 'approved', synonyms: ['Sales', 'Income'] },
  { id: '2', term: 'COGS', definition: 'Cost of goods sold', source: 'llm_approved', status: 'approved', synonyms: ['Cost'] },
  { id: '3', term: 'Churn', definition: 'Customer cancellation rate', source: 'llm', status: 'pending', synonyms: [] },
];

describe('GlossaryModal', () => {
  it('renders all entries when open', () => {
    render(<GlossaryModal open={true} onClose={vi.fn()} entries={mockEntries} />);
    expect(screen.getByText('Revenue')).toBeDefined();
    expect(screen.getByText('COGS')).toBeDefined();
    expect(screen.getByText('Churn')).toBeDefined();
  });

  it('shows entry definitions', () => {
    render(<GlossaryModal open={true} onClose={vi.fn()} entries={mockEntries} />);
    expect(screen.getByText('Total income from sales')).toBeDefined();
  });

  it('shows empty state when no entries', () => {
    render(<GlossaryModal open={true} onClose={vi.fn()} entries={[]} />);
    expect(screen.getByText(/no glossary entries/i)).toBeDefined();
  });

  it('does not render content when closed', () => {
    const { container } = render(<GlossaryModal open={false} onClose={vi.fn()} entries={mockEntries} />);
    expect(screen.queryByText('Revenue')).toBeNull();
  });
});
