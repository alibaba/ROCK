/**
 * Tests for job/trial/compose.ts — ComposeTrial
 */

import { ComposeTrial } from './compose';
import { TrialResult } from '../result';
import { ComposeJobConfig } from '../config_compose';

function makeComposeConfig(overrides?: Partial<ComposeJobConfig>): ComposeJobConfig {
  return {
    job_name: 'compose-test',
    labels: {},
    environment: { env: {}, uploads: [] },
    namespace: null,
    experiment_id: null,
    timeout: 7200,
    services: [
      {
        name: 'main',
        image: 'myapp:latest',
        command: null, args: null, script: null, env: {},
        ports: [], resources: null, privileged: false,
        volume_mounts: [], is_main: true,
      },
    ],
    init_containers: [],
    volumes: [],
    oss_artifacts: [],
    network_mode: 'host',
    callback_url: null,
    ...overrides,
  };
}

describe('ComposeTrial', () => {
  describe('constructor', () => {
    test('stores ComposeJobConfig', () => {
      const config = makeComposeConfig();
      const trial = new ComposeTrial(config as any);
      expect(trial.config.job_name).toBe('compose-test');
    });
  });

  describe('build', () => {
    test('generates runner script via builder', () => {
      const config = makeComposeConfig();
      const trial = new ComposeTrial(config as any);
      const script = trial.build();

      expect(script).toContain('#!/bin/bash');
      expect(script).toContain('dockerd');
      expect(script).toContain('compose-test');
    });
  });

  describe('collect', () => {
    test('returns TrialResult with success for exit_code 0', async () => {
      const config = makeComposeConfig();
      const trial = new ComposeTrial(config as any);
      const result = await trial.collect(undefined, 'output', 0) as TrialResult;

      expect(result.exit_code).toBe(0);
      expect(result.exception_info).toBeNull();
    });

    test('maps exit_code 90 to DockerdStartupTimeout', async () => {
      const config = makeComposeConfig();
      const trial = new ComposeTrial(config as any);
      const result = await trial.collect(undefined, 'timeout output', 90) as TrialResult;

      expect(result.exit_code).toBe(90);
      expect(result.exception_info?.exception_type).toBe('DockerdStartupTimeout');
    });

    test('maps exit_code 91 to ComposeUpFailed', async () => {
      const config = makeComposeConfig();
      const trial = new ComposeTrial(config as any);
      const result = await trial.collect(undefined, 'compose error', 91) as TrialResult;

      expect(result.exception_info?.exception_type).toBe('ComposeUpFailed');
    });

    test('maps exit_code 92 to InitContainerFailed', async () => {
      const config = makeComposeConfig();
      const trial = new ComposeTrial(config as any);
      const result = await trial.collect(undefined, 'init error', 92) as TrialResult;

      expect(result.exception_info?.exception_type).toBe('InitContainerFailed');
    });

    test('uses generic ComposeExitCode for other codes', async () => {
      const config = makeComposeConfig();
      const trial = new ComposeTrial(config as any);
      const result = await trial.collect(undefined, 'generic error', 137) as TrialResult;

      expect(result.exception_info?.exception_type).toBe('ComposeExitCode');
    });
  });
});
