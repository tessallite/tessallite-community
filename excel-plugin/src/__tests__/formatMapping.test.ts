import { describe, it, expect } from 'vitest';

const FORMAT_MAP: Record<string, string> = {
  currency: '$#,##0.00',
  currency_0dp: '$#,##0',
  percent: '0.00%',
  percent_0dp: '0%',
  decimal_2dp: '#,##0.00',
  decimal_0dp: '#,##0',
};

function mapFormatToken(token: string | null | undefined): string {
  if (!token) return 'General';
  return FORMAT_MAP[token] || 'General';
}

function buildFormatTokens(
  annotation?: {
    measures?: Record<string, { title: string; type: string; format?: string }>;
  },
): Record<string, string> {
  const tokens: Record<string, string> = {};
  if (annotation?.measures) {
    for (const [key, m] of Object.entries(annotation.measures)) {
      if (m.format) {
        tokens[key] = mapFormatToken(m.format);
        if (m.title) tokens[m.title] = mapFormatToken(m.format);
      }
    }
  }
  return tokens;
}

describe('format token mapping', () => {
  it('maps currency to $#,##0.00', () => {
    expect(mapFormatToken('currency')).toBe('$#,##0.00');
  });

  it('maps currency_0dp to $#,##0', () => {
    expect(mapFormatToken('currency_0dp')).toBe('$#,##0');
  });

  it('maps percent to 0.00%', () => {
    expect(mapFormatToken('percent')).toBe('0.00%');
  });

  it('maps percent_0dp to 0%', () => {
    expect(mapFormatToken('percent_0dp')).toBe('0%');
  });

  it('maps decimal_2dp to #,##0.00', () => {
    expect(mapFormatToken('decimal_2dp')).toBe('#,##0.00');
  });

  it('maps decimal_0dp to #,##0', () => {
    expect(mapFormatToken('decimal_0dp')).toBe('#,##0');
  });

  it('maps unknown token to General', () => {
    expect(mapFormatToken('unknown_format')).toBe('General');
  });

  it('maps null to General', () => {
    expect(mapFormatToken(null)).toBe('General');
  });

  it('maps undefined to General', () => {
    expect(mapFormatToken(undefined)).toBe('General');
  });

  it('builds format tokens from annotation by key', () => {
    const tokens = buildFormatTokens({
      measures: {
        'rev': { title: 'Revenue', type: 'number', format: 'currency' },
        'pct': { title: 'Percent', type: 'number', format: 'percent' },
      },
    });
    expect(tokens['rev']).toBe('$#,##0.00');
    expect(tokens['Revenue']).toBe('$#,##0.00');
    expect(tokens['pct']).toBe('0.00%');
    expect(tokens['Percent']).toBe('0.00%');
  });

  it('skips measures without format tokens', () => {
    const tokens = buildFormatTokens({
      measures: {
        'cnt': { title: 'Count', type: 'number' },
        'rev': { title: 'Revenue', type: 'number', format: 'currency' },
      },
    });
    expect(tokens['cnt']).toBeUndefined();
    expect(tokens['Count']).toBeUndefined();
    expect(tokens['rev']).toBe('$#,##0.00');
  });

  it('returns empty object for missing annotation', () => {
    const tokens = buildFormatTokens(undefined);
    expect(tokens).toEqual({});
  });
});
