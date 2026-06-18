import { z } from 'zod';
import { MetricType, MetricTypeSchema } from './type';

describe('MetricType', () => {
  test('has all expected values', () => {
    expect(MetricType.SUM).toBe('sum');
    expect(MetricType.MIN).toBe('min');
    expect(MetricType.MAX).toBe('max');
    expect(MetricType.MEAN).toBe('mean');
    expect(MetricType.UV_SCRIPT).toBe('uv-script');
  });

  describe('MetricTypeSchema', () => {
    test('parses valid values', () => {
      expect(MetricTypeSchema.parse('sum')).toBe('sum');
      expect(MetricTypeSchema.parse('mean')).toBe('mean');
      expect(MetricTypeSchema.parse('uv-script')).toBe('uv-script');
    });

    test('rejects invalid values', () => {
      expect(() => MetricTypeSchema.parse('invalid')).toThrow(z.ZodError);
      expect(() => MetricTypeSchema.parse('')).toThrow(z.ZodError);
    });
  });
});
