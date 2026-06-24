import { describe, it, expect } from 'vitest';
import { checkTemplatePrerequisites, REPORT_TEMPLATES } from '../utils/reportTemplates';

describe('reportTemplates', () => {
  describe('REPORT_TEMPLATES', () => {
    it('defines 6 templates', () => {
      expect(REPORT_TEMPLATES).toHaveLength(6);
    });

    it('all templates have unique ids', () => {
      const ids = REPORT_TEMPLATES.map(t => t.id);
      expect(new Set(ids).size).toBe(ids.length);
    });

    it('all templates have required fields', () => {
      for (const t of REPORT_TEMPLATES) {
        expect(t.id).toBeTruthy();
        expect(t.name).toBeTruthy();
        expect(t.description).toBeTruthy();
        expect(t.icon).toBeTruthy();
      }
    });
  });

  describe('checkTemplatePrerequisites', () => {
    it('returns valid when all prerequisites met', () => {
      const result = checkTemplatePrerequisites(
        REPORT_TEMPLATES.find(t => t.id === 'time-series')!,
        3,
        true,
        false,
      );
      expect(result.valid).toBe(true);
      expect(result.missing).toHaveLength(0);
    });

    it('reports missing measure', () => {
      const result = checkTemplatePrerequisites(
        REPORT_TEMPLATES.find(t => t.id === 'time-series')!,
        0,
        true,
        false,
      );
      expect(result.valid).toBe(false);
      expect(result.missing).toContainEqual(expect.stringContaining('measure'));
    });

    it('reports missing time dimension', () => {
      const result = checkTemplatePrerequisites(
        REPORT_TEMPLATES.find(t => t.id === 'time-series')!,
        3,
        false,
        false,
      );
      expect(result.valid).toBe(false);
      expect(result.missing).toContainEqual(expect.stringContaining('time dimension'));
    });

    it('reports missing comparison measure for variance', () => {
      const result = checkTemplatePrerequisites(
        REPORT_TEMPLATES.find(t => t.id === 'variance')!,
        1,
        false,
        true,
      );
      expect(result.valid).toBe(false);
      expect(result.missing.length).toBeGreaterThan(0);
    });

    it('kpi-snapshot only requires one measure', () => {
      const result = checkTemplatePrerequisites(
        REPORT_TEMPLATES.find(t => t.id === 'kpi-snapshot')!,
        1,
        false,
        false,
      );
      expect(result.valid).toBe(true);
    });
  });
});
