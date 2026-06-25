/**
 * Tests for job/operator.ts — Operator, ScatterOperator
 */

import { z } from 'zod';
import { SandboxConfigSchema } from '../sandbox/config';
import { BashJobConfigSchema } from './config';
import { ScatterOperator, Operator } from './operator';
import { AbstractTrial } from './trial/abstract';
import { registerTrial } from './trial/registry';

// Minimal environment
const EnvironmentConfigSchema = SandboxConfigSchema.extend({
  uploads: z.array(z.tuple([z.string(), z.string()])).default([]),
  env: z.record(z.string()).default({}),
  oss_mirror: z.any().nullable().default(null),
  proxy: z.any().nullable().default(null),
  tracking: z.any().nullable().default(null),
});

// Test trial
const BASH_KEY = Symbol.for('TestBashJobConfig');
class TestTrial extends AbstractTrial {
  build(): string { return 'echo test'; }
  async collect(): Promise<any> { return []; }
}
registerTrial(BASH_KEY, TestTrial);

function makeConfig(): Record<string, unknown> {
  const schema = BashJobConfigSchema(EnvironmentConfigSchema);
  const config = schema.parse({ script: 'echo hi' }) as Record<string, unknown>;
  (config as any)['_registryKey'] = BASH_KEY;
  return config;
}

// ---------------------------------------------------------------------------
// ScatterOperator
// ---------------------------------------------------------------------------
describe('ScatterOperator', () => {
  describe('constructor', () => {
    test('defaults to size 1', () => {
      const op = new ScatterOperator();
      expect(op.size).toBe(1);
    });

    test('accepts custom size', () => {
      const op = new ScatterOperator(8);
      expect(op.size).toBe(8);
    });
  });

  describe('apply', () => {
    test('returns empty array when size is 0', () => {
      const config = makeConfig();
      const op = new ScatterOperator(0);
      const trials = op.apply(config);
      expect(trials).toEqual([]);
    });

    test('returns empty array when size is negative', () => {
      const config = makeConfig();
      const op = new ScatterOperator(-1);
      const trials = op.apply(config);
      expect(trials).toEqual([]);
    });

    test('returns single trial for default size=1', () => {
      const config = makeConfig();
      const op = new ScatterOperator();
      const trials = op.apply(config);
      expect(trials).toHaveLength(1);
      expect(trials[0]).toBeInstanceOf(TestTrial);
    });

    test('returns N trials for size=N (same trial object)', () => {
      const config = makeConfig();
      const op = new ScatterOperator(5);
      const trials = op.apply(config);
      expect(trials).toHaveLength(5);
      // All should be the same instance (scatter pattern)
      expect(trials[0]).toBe(trials[1]);
    });
  });
});
