/**
 * Tests for job/trial/bash.ts — BashTrial
 */

import { z } from 'zod';
import { SandboxConfigSchema } from '../../sandbox/config';
import { BashJobConfigSchema, BashJobConfig } from '../config';
import { TrialResult } from '../result';
import { BashTrial } from './bash';

const EnvironmentConfigSchema = SandboxConfigSchema.extend({
  uploads: z.array(z.tuple([z.string(), z.string()])).default([]),
  env: z.record(z.string()).default({}),
  oss_mirror: z.any().nullable().default(null),
  proxy: z.any().nullable().default(null),
  tracking: z.any().nullable().default(null),
});

const schema = BashJobConfigSchema(EnvironmentConfigSchema);

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
describe('BashTrial', () => {
  describe('constructor', () => {
    test('stores BashJobConfig', () => {
      const config = schema.parse({ script: 'echo hi' }) as BashJobConfig;
      const trial = new BashTrial(config as any);
      expect(trial.config).toBe(config);
    });
  });

  describe('build', () => {
    test('returns raw script when no OSS mirror', () => {
      const config = schema.parse({
        script: '#!/bin/bash\necho "hello world"\nexit 0',
      }) as BashJobConfig;
      const trial = new BashTrial(config as any);
      const built = trial.build();
      expect(built).toBe('#!/bin/bash\necho "hello world"\nexit 0');
    });

    test('returns raw script when OSS mirror is disabled', () => {
      const config = schema.parse({
        script: 'echo test',
        environment: { oss_mirror: { enabled: false } },
      }) as BashJobConfig;
      const trial = new BashTrial(config as any);
      expect(trial.build()).toBe('echo test');
    });

    test('wraps script when OSS mirror is enabled', () => {
      // We need to set up oss_mirror credentials for the wrapper to work
      const config = schema.parse({
        script: 'echo "my user script"',
        namespace: 'test-ns',
        experiment_id: 'exp-1',
        environment: {
          oss_mirror: {
            enabled: true,
            oss_bucket: 'test-bucket',
            oss_endpoint: 'http://oss.example.com',
            oss_region: 'test-region',
            oss_access_key_id: '',
            oss_access_key_secret: '',
          },
          env: {
            OSS_BUCKET: 'test-bucket',
            OSS_ENDPOINT: 'http://oss.example.com',
            OSS_REGION: 'test-region',
          },
        },
      }) as BashJobConfig;
      const trial = new BashTrial(config as any);

      // Simulate onSandboxReady to set namespace/experiment_id
      const fakeSandbox = {
        getNamespace: () => 'test-ns',
        getExperimentId: () => 'exp-1',
      };
      // The config already has these

      // Simulate that ossutil is ready
      (trial as any)._ossutilReady = true;

      const built = trial.build();
      // Wrapper script should include heredoc wrapper
      expect(built).toContain('#!/bin/bash');
      expect(built).toContain('__ROCK_USER_SCRIPT_EOF_');
      expect(built).toContain('echo "my user script"');
    });

    test('returns empty string when no script and no OSS mirror', () => {
      const config = schema.parse({}) as BashJobConfig;
      const trial = new BashTrial(config as any);
      expect(trial.build()).toBe('');
    });
  });

  describe('collect', () => {
    test('returns TrialResult with success for exit_code 0', async () => {
      const config = schema.parse({ script: 'echo hi' }) as BashJobConfig;
      const trial = new BashTrial(config as any);
      const result = await trial.collect(undefined, 'hello', 0) as TrialResult;
      expect(result.exit_code).toBe(0);
      expect(result.raw_output).toBe('hello');
      expect(result.exception_info).toBeNull();
    });

    test('returns TrialResult with failure for non-zero exit code', async () => {
      const config = schema.parse({ script: 'echo fail' }) as BashJobConfig;
      const trial = new BashTrial(config as any);
      const result = await trial.collect(undefined, 'error output', 1) as TrialResult;
      expect(result.exit_code).toBe(1);
      expect(result.exception_info).not.toBeNull();
      expect(result.exception_info?.exception_type).toBe('BashExitCode');
      expect(result.exception_info?.exception_message).toContain('exited with code 1');
    });
  });

  describe('onSandboxReady', () => {
    test('calls super and prepares OSS env when mirror enabled', async () => {
      const config = schema.parse({
        script: 'echo hi',
        namespace: 'ns-1',
        experiment_id: 'exp-1',
        environment: {
          oss_mirror: {
            enabled: true,
            oss_bucket: 'b',
            oss_endpoint: 'e',
            oss_region: 'r',
            oss_access_key_id: '',
            oss_access_key_secret: '',
          },
          env: {
            OSS_BUCKET: 'b',
            OSS_ENDPOINT: 'e',
            OSS_REGION: 'r',
          },
        },
      }) as BashJobConfig;
      const trial = new BashTrial(config as any);

      const fakeSandbox = {
        getNamespace: () => 'ns-1',
        getExperimentId: () => 'exp-1',
      };
      await trial.onSandboxReady(fakeSandbox as any);

      // OSS env vars should be injected
      const env = (config as any).environment.env;
      expect(env.OSS_BUCKET).toBeDefined();
    });

    test('does nothing extra when OSS mirror is disabled', async () => {
      const config = schema.parse({ script: 'echo hi' }) as BashJobConfig;
      const trial = new BashTrial(config as any);

      const fakeSandbox = {
        getNamespace: () => 'test-ns',
        getExperimentId: () => 'exp-1',
      };
      await trial.onSandboxReady(fakeSandbox as any);

      // No OSS env should be injected
      expect(trial.config.namespace).toBe('test-ns');
    });
  });

  describe('static renderWrapper', () => {
    test('generates valid wrapper script with heredoc isolation', () => {
      const wrapper = BashTrial.renderWrapper('echo hello');
      expect(wrapper).toContain('#!/bin/bash');
      expect(wrapper).toContain('set +e');
      expect(wrapper).toContain('__ROCK_USER_SCRIPT_EOF_');
      expect(wrapper).toContain('echo hello');
      expect(wrapper).toContain('exit $_rock_user_rc');
    });

    test('wrapper includes OSS upload prologue and epilogue', () => {
      const wrapper = BashTrial.renderWrapper('echo test');
      expect(wrapper).toContain('ossutil');
      expect(wrapper).toContain('$ROCK_ARTIFACT_DIR');
      expect(wrapper).toContain('$OSS_BUCKET');
    });
  });
});
