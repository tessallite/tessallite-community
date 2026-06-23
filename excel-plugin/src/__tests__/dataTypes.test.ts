/**
 * M-2 / Bug-1061 — physical-to-semantic data-type classification.
 *
 * The Report Builder's template gating compared `data_type === 'string'`, which
 * never matches the physical type spellings that real models return
 * ("character varying", "boolean", "date"). These tests pin the classifier to
 * the PG spellings plus common BigQuery/Snowflake spellings so the top-n /
 * geographic templates light up on real models.
 */
import { describe, it, expect } from 'vitest';
import { classifyDataType, isTextualType } from '../utils/dataTypes';

describe('classifyDataType', () => {
  it('classifies PostgreSQL physical text types as text', () => {
    expect(classifyDataType('character varying')).toBe('text');
    expect(classifyDataType('varchar(255)')).toBe('text');
    expect(classifyDataType('text')).toBe('text');
    expect(classifyDataType('char(2)')).toBe('text');
    expect(classifyDataType('uuid')).toBe('text');
  });

  it('classifies the canonical semantic word "string" and warehouse spellings as text', () => {
    expect(classifyDataType('string')).toBe('text');      // canonical / BigQuery STRING
    expect(classifyDataType('STRING')).toBe('text');
    expect(classifyDataType('VARCHAR')).toBe('text');      // Snowflake
  });

  it('classifies boolean spellings as boolean (not numeric)', () => {
    expect(classifyDataType('boolean')).toBe('boolean');
    expect(classifyDataType('bool')).toBe('boolean');
    expect(classifyDataType('bit')).toBe('boolean');
    expect(classifyDataType('BOOL')).toBe('boolean');
  });

  it('classifies date/time spellings as date', () => {
    expect(classifyDataType('date')).toBe('date');
    expect(classifyDataType('timestamp without time zone')).toBe('date');
    expect(classifyDataType('timestamptz')).toBe('date');
    expect(classifyDataType('DATETIME')).toBe('date');     // BigQuery
  });

  it('classifies numeric spellings across warehouses as numeric', () => {
    expect(classifyDataType('integer')).toBe('numeric');
    expect(classifyDataType('bigint')).toBe('numeric');
    expect(classifyDataType('numeric(18,2)')).toBe('numeric');
    expect(classifyDataType('double precision')).toBe('numeric');
    expect(classifyDataType('INT64')).toBe('numeric');     // BigQuery
    expect(classifyDataType('FLOAT64')).toBe('numeric');   // BigQuery
    expect(classifyDataType('NUMBER')).toBe('numeric');    // Snowflake
  });

  it('returns unknown for null/empty/unrecognised', () => {
    expect(classifyDataType(null)).toBe('unknown');
    expect(classifyDataType(undefined)).toBe('unknown');
    expect(classifyDataType('')).toBe('unknown');
    expect(classifyDataType('geography')).toBe('unknown');
  });
});

describe('isTextualType', () => {
  it('is true for textual physical types and false for non-text', () => {
    expect(isTextualType('character varying')).toBe(true);
    expect(isTextualType('string')).toBe(true);
    expect(isTextualType('boolean')).toBe(false);
    expect(isTextualType('date')).toBe(false);
    expect(isTextualType('numeric')).toBe(false);
    expect(isTextualType(null)).toBe(false);
  });
});
