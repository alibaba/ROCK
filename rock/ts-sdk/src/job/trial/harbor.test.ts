/**
 * Tests for job/trial/harbor.ts — HarborTrial
 */

import { HarborTrial } from './harbor';
import { TrialResult } from '../result';

// Minimal HarborJobConfig-like shape for testing
// We don't import the real HarborJobConfig to avoid bench circular deps
function makeHarborConfig(overrides?: Record<string, unknown>): Record<string, unknown> {
  return {
    environment: {
      image: 'test:latest',
      uploads: [],
      env: {},
      oss_mirror: null,
      proxy: null,
      tracking: null,
    },
    job_name: 'harbor-test',
    namespace: null,
    experiment_id: 'exp-1',
    labels: {},
    timeout: 7200,
    jobs_dir: '/data/logs/user-defined/jobs',
    ...overrides,
  };
}

describe('HarborTrial', () => {
  describe('constructor', () => {
    test('stores config', () => {
      const config = makeHarborConfig();
      const trial = new HarborTrial(config);
      expect(trial.config.job_name).toBe('harbor-test');
      expect(trial.config.experiment_id).toBe('exp-1');
    });
  });

  describe('build', () => {
    test('generates docker + harbor jobs start script', () => {
      const config = makeHarborConfig();
      const trial = new HarborTrial(config);
      const script = trial.build();

      expect(script).toContain('#!/bin/bash');
      expect(script).toContain('set -e');
      expect(script).toContain('dockerd');
      expect(script).toContain('docker info');
      expect(script).toContain('harbor jobs start -c');
    });

    test('includes config path in script', () => {
      const config = makeHarborConfig();
      const trial = new HarborTrial(config);
      const script = trial.build();
      // Config path includes user_defined_dir
      expect(script).toContain('/data/logs/user-defined');
    });
  });

  describe('collect', () => {
    test('returns array of TrialResult', async () => {
      const config = makeHarborConfig();
      const trial = new HarborTrial(config);

      // Without a real sandbox/docker, collect returns synthetic results
      const results = await trial.collect(undefined, '', 0);
      expect(Array.isArray(results)).toBe(true);
    });

    test('returns synthetic failure when no trial results found', async () => {
      const config = makeHarborConfig();
      const trial = new HarborTrial(config);

      const results = await trial.collect(undefined, '', 0) as TrialResult[];
      expect(results.length).toBeGreaterThanOrEqual(1);
      expect(results[0]!.exception_info).not.toBeNull();
      expect(results[0]!.exception_info?.exception_type).toBe('HarborNoTrials');
    });
  });
});
