import { z } from 'zod';
import {
  EnvironmentType,
  EnvironmentTypeSchema,
} from './environment_type';

describe('EnvironmentType', () => {
  test('has all expected values', () => {
    expect(EnvironmentType.DOCKER).toBe('docker');
    expect(EnvironmentType.DAYTONA).toBe('daytona');
    expect(EnvironmentType.E2B).toBe('e2b');
    expect(EnvironmentType.MODAL).toBe('modal');
    expect(EnvironmentType.RUNLOOP).toBe('runloop');
    expect(EnvironmentType.GKE).toBe('gke');
    expect(EnvironmentType.ROCK).toBe('rock');
  });

  test('is a const object with string values', () => {
    expect(typeof EnvironmentType.DOCKER).toBe('string');
  });

  describe('EnvironmentTypeSchema', () => {
    test('parses valid values', () => {
      expect(EnvironmentTypeSchema.parse('docker')).toBe('docker');
      expect(EnvironmentTypeSchema.parse('rock')).toBe('rock');
      expect(EnvironmentTypeSchema.parse('gke')).toBe('gke');
    });

    test('rejects invalid values', () => {
      expect(() => EnvironmentTypeSchema.parse('invalid')).toThrow(z.ZodError);
      expect(() => EnvironmentTypeSchema.parse('')).toThrow(z.ZodError);
      expect(() => EnvironmentTypeSchema.parse(123)).toThrow(z.ZodError);
    });
  });
});
