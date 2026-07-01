/**
 * Tests for job/trial/registry.ts — register_trial, create_trial
 */

import { z } from 'zod';
import { SandboxConfigSchema } from '../../sandbox/config';
import { BashJobConfigSchema } from '../config';
import { AbstractTrial } from './abstract';
import { registerTrial, createTrial, _assignRegistryKey } from './registry';

// Minimal environment schema
const EnvironmentConfigSchema = SandboxConfigSchema.extend({
  uploads: z.array(z.tuple([z.string(), z.string()])).default([]),
  env: z.record(z.string()).default({}),
  oss_mirror: z.any().nullable().default(null),
  proxy: z.any().nullable().default(null),
  tracking: z.any().nullable().default(null),
});

// Registry key for BashJobConfig
export const BASH_JOB_CONFIG_KEY = Symbol.for('BashJobConfig');

// Test Trial subclass
class TestBashTrial extends AbstractTrial {
  build(): string { return '#!/bin/bash\necho test'; }
  async collect(): Promise<any> { return { task_name: 'test' }; }
}

describe('Registry', () => {
  describe('registerTrial', () => {
    test('registers a trial class for a config key', () => {
      // Should not throw
      registerTrial(BASH_JOB_CONFIG_KEY, TestBashTrial);
    });
  });

  describe('createTrial', () => {
    test('creates a trial instance from registered config', () => {
      registerTrial(BASH_JOB_CONFIG_KEY, TestBashTrial);

      const schema = BashJobConfigSchema(EnvironmentConfigSchema);
      const config = schema.parse({ script: 'echo hi' }) as Record<string, unknown>;
      _assignRegistryKey(config, BASH_JOB_CONFIG_KEY);

      const trial = createTrial(config);
      expect(trial).toBeInstanceOf(TestBashTrial);
      expect(trial.config).toBe(config);
    });

    test('throws TypeError for unregistered config type', () => {
      const schema = BashJobConfigSchema(EnvironmentConfigSchema);
      const config = schema.parse({ script: 'echo hi' }) as Record<string, unknown>;
      _assignRegistryKey(config, Symbol.for('UnknownConfig'));

      expect(() => createTrial(config)).toThrow(TypeError);
    });

    test('throws with helpful message about missing registry key', () => {
      const schema = BashJobConfigSchema(EnvironmentConfigSchema);
      const config = schema.parse({ script: 'echo hi' }) as Record<string, unknown>;
      // No _registryKey assigned

      try {
        createTrial(config);
        // Should not reach here
        expect(true).toBe(false);
      } catch (e: any) {
        expect(e.message).toContain('_registryKey');
      }
    });
  });
});
