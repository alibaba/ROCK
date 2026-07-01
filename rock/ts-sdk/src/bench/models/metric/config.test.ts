import { z } from 'zod';
import {
  MetricConfig,
  MetricConfigSchema,
  createMetricConfig,
} from './config';
import { MetricType } from './type';

describe('MetricConfig', () => {
  describe('MetricConfigSchema', () => {
    test('parses empty object with defaults', () => {
      const result = MetricConfigSchema.parse({});
      expect(result.type).toBe(MetricType.MEAN);
      expect(result.kwargs).toEqual({});
    });

    test('parses explicit type and kwargs', () => {
      const result = MetricConfigSchema.parse({
        type: MetricType.SUM,
        kwargs: { key: 'value' },
      });
      expect(result.type).toBe(MetricType.SUM);
      expect(result.kwargs).toEqual({ key: 'value' });
    });

    test('rejects invalid type', () => {
      expect(() => MetricConfigSchema.parse({ type: 'invalid' })).toThrow(
        z.ZodError
      );
    });

    test('rejects non-object kwargs', () => {
      expect(() =>
        MetricConfigSchema.parse({ kwargs: 'not-an-object' })
      ).toThrow(z.ZodError);
    });
  });

  describe('createMetricConfig', () => {
    test('creates with all defaults when called with no args', () => {
      const result = createMetricConfig();
      expect(result.type).toBe(MetricType.MEAN);
      expect(result.kwargs).toEqual({});
    });

    test('creates with partial overrides', () => {
      const result = createMetricConfig({ type: MetricType.MAX });
      expect(result.type).toBe(MetricType.MAX);
      expect(result.kwargs).toEqual({});
    });

    test('creates with undefined input treated as defaults', () => {
      const result = createMetricConfig(undefined);
      expect(result.type).toBe(MetricType.MEAN);
    });
  });
});
