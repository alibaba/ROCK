import { z } from 'zod';
import {
  OrchestratorType,
  OrchestratorTypeSchema,
} from './orchestrator_type';

describe('OrchestratorType', () => {
  test('has all expected values', () => {
    expect(OrchestratorType.LOCAL).toBe('local');
    expect(OrchestratorType.QUEUE).toBe('queue');
  });

  describe('OrchestratorTypeSchema', () => {
    test('parses valid values', () => {
      expect(OrchestratorTypeSchema.parse('local')).toBe('local');
      expect(OrchestratorTypeSchema.parse('queue')).toBe('queue');
    });

    test('rejects invalid values', () => {
      expect(() => OrchestratorTypeSchema.parse('invalid')).toThrow(z.ZodError);
      expect(() => OrchestratorTypeSchema.parse('')).toThrow(z.ZodError);
    });
  });
});
