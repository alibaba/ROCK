import { z } from 'zod';

/** Metric aggregation type for Harbor benchmark evaluation. */
export const MetricType = {
  SUM: 'sum',
  MIN: 'min',
  MAX: 'max',
  MEAN: 'mean',
  UV_SCRIPT: 'uv-script',
} as const;

export type MetricType = (typeof MetricType)[keyof typeof MetricType];

/** Zod schema for runtime validation of MetricType values. */
export const MetricTypeSchema = z.nativeEnum(MetricType);
