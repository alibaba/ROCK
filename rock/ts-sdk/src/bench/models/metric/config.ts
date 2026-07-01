import { z } from 'zod';
import { MetricTypeSchema, MetricType } from './type.js';

/** Zod schema for MetricConfig — controls how trial-level metrics are aggregated. */
export const MetricConfigSchema = z.object({
  type: MetricTypeSchema.default(MetricType.MEAN),
  kwargs: z.record(z.unknown()).default({}),
});

export type MetricConfig = z.infer<typeof MetricConfigSchema>;

/** Create a MetricConfig with defaults applied. */
export function createMetricConfig(config?: Partial<MetricConfig>): MetricConfig {
  return MetricConfigSchema.parse(config ?? {});
}
