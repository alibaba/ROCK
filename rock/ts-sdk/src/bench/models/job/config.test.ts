import { z } from 'zod';
import {
  RetryConfig,
  RetryConfigSchema,
  createRetryConfig,
  OrchestratorConfig,
  OrchestratorConfigSchema,
  createOrchestratorConfig,
  OssRegistryInfoSchema,
  RemoteRegistryInfoSchema,
  HFRegistryInfoSchema,
  LocalRegistryInfoSchema,
  LocalDatasetConfigSchema,
  RegistryDatasetConfigSchema,
  DatasetConfigSchema,
  parseDatasetConfig,
  HarborJobConfigSchema,
  createHarborJobConfig,
} from './config';
import { OrchestratorType } from '../orchestrator_type';

// ---------------------------------------------------------------------------
// RetryConfig
// ---------------------------------------------------------------------------
describe('RetryConfig', () => {
  describe('RetryConfigSchema', () => {
    test('parses empty object with defaults', () => {
      const result = RetryConfigSchema.parse({});
      expect(result.max_retries).toBe(0);
      expect(result.include_exceptions).toBeNull();
      expect(result.exclude_exceptions).toEqual([
        'AgentTimeoutError',
        'VerifierTimeoutError',
        'RewardFileNotFoundError',
        'RewardFileEmptyError',
        'VerifierOutputParseError',
      ]);
      expect(result.wait_multiplier).toBe(1.0);
      expect(result.min_wait_sec).toBe(1.0);
      expect(result.max_wait_sec).toBe(60.0);
    });

    test('parses custom retry settings', () => {
      const result = RetryConfigSchema.parse({
        max_retries: 3,
        include_exceptions: ['CustomError'],
        wait_multiplier: 2.0,
      });
      expect(result.max_retries).toBe(3);
      expect(result.include_exceptions).toEqual(['CustomError']);
      expect(result.wait_multiplier).toBe(2.0);
    });

    test('rejects negative max_retries', () => {
      expect(() => RetryConfigSchema.parse({ max_retries: -1 })).toThrow(
        z.ZodError
      );
    });
  });

  describe('createRetryConfig', () => {
    test('creates with defaults', () => {
      const result = createRetryConfig();
      expect(result.max_retries).toBe(0);
    });

    test('creates with overrides', () => {
      const result = createRetryConfig({ max_retries: 5 });
      expect(result.max_retries).toBe(5);
    });
  });
});

// ---------------------------------------------------------------------------
// OrchestratorConfig
// ---------------------------------------------------------------------------
describe('OrchestratorConfig', () => {
  describe('OrchestratorConfigSchema', () => {
    test('parses empty object with defaults', () => {
      const result = OrchestratorConfigSchema.parse({});
      expect(result.type).toBe(OrchestratorType.LOCAL);
      expect(result.n_concurrent_trials).toBe(4);
      expect(result.quiet).toBe(false);
      expect(result.retry).toEqual(createRetryConfig());
      expect(result.kwargs).toEqual({});
    });

    test('parses queue orchestrator', () => {
      const result = OrchestratorConfigSchema.parse({
        type: OrchestratorType.QUEUE,
        n_concurrent_trials: 8,
        quiet: true,
      });
      expect(result.type).toBe(OrchestratorType.QUEUE);
      expect(result.n_concurrent_trials).toBe(8);
      expect(result.quiet).toBe(true);
    });

    test('parses with custom retry', () => {
      const result = OrchestratorConfigSchema.parse({
        retry: { max_retries: 3, wait_multiplier: 1.5 },
      });
      expect(result.retry.max_retries).toBe(3);
      expect(result.retry.wait_multiplier).toBe(1.5);
    });
  });

  describe('createOrchestratorConfig', () => {
    test('creates with defaults', () => {
      const result = createOrchestratorConfig();
      expect(result.type).toBe(OrchestratorType.LOCAL);
    });
  });
});

// ---------------------------------------------------------------------------
// Registry info models
// ---------------------------------------------------------------------------
describe('Registry info models', () => {
  describe('OssRegistryInfoSchema', () => {
    test('parses empty object with null defaults', () => {
      const result = OssRegistryInfoSchema.parse({});
      expect(result.split).toBeNull();
      expect(result.revision).toBeNull();
      expect(result.oss_dataset_path).toBeNull();
      expect(result.oss_access_key_id).toBeNull();
      expect(result.oss_access_key_secret).toBeNull();
      expect(result.oss_region).toBeNull();
      expect(result.oss_endpoint).toBeNull();
      expect(result.oss_bucket).toBeNull();
    });

    test('parses OSS config with all fields', () => {
      const result = OssRegistryInfoSchema.parse({
        split: 'train',
        revision: 'v1',
        oss_dataset_path: '/datasets/test',
        oss_access_key_id: 'key-id',
        oss_access_key_secret: 'secret',
        oss_region: 'us-east-1',
        oss_endpoint: 'https://oss.example.com',
        oss_bucket: 'my-bucket',
      });
      expect(result.split).toBe('train');
      expect(result.revision).toBe('v1');
      expect(result.oss_bucket).toBe('my-bucket');
    });
  });

  describe('RemoteRegistryInfoSchema', () => {
    test('parses empty object with default URL', () => {
      const result = RemoteRegistryInfoSchema.parse({});
      expect(result.name).toBeNull();
      expect(result.url).toBe(
        'https://raw.githubusercontent.com/laude-institute/harbor/main/registry.json'
      );
    });

    test('parses custom URL', () => {
      const result = RemoteRegistryInfoSchema.parse({
        name: 'custom',
        url: 'https://example.com/registry.json',
      });
      expect(result.name).toBe('custom');
      expect(result.url).toBe('https://example.com/registry.json');
    });
  });

  describe('HFRegistryInfoSchema', () => {
    test('parses empty object with null defaults', () => {
      const result = HFRegistryInfoSchema.parse({});
      expect(result.split).toBeNull();
      expect(result.revision).toBeNull();
    });

    test('parses HF registry info', () => {
      const result = HFRegistryInfoSchema.parse({
        split: 'train',
        revision: 'main',
      });
      expect(result.split).toBe('train');
      expect(result.revision).toBe('main');
    });
  });

  describe('LocalRegistryInfoSchema', () => {
    test('parses local registry with path', () => {
      const result = LocalRegistryInfoSchema.parse({ path: '/data/registry' });
      expect(result.name).toBeNull();
      expect(result.path).toBe('/data/registry');
    });

    test('requires path', () => {
      expect(() => LocalRegistryInfoSchema.parse({})).toThrow(z.ZodError);
    });
  });
});

// ---------------------------------------------------------------------------
// Dataset configs
// ---------------------------------------------------------------------------
describe('Dataset configs', () => {
  describe('LocalDatasetConfigSchema', () => {
    test('parses minimal local dataset', () => {
      const result = LocalDatasetConfigSchema.parse({ kind: 'local' as const, path: '/data/datasets/test' });
      expect(result.path).toBe('/data/datasets/test');
      expect(result.task_names).toBeNull();
      expect(result.exclude_task_names).toBeNull();
      expect(result.n_tasks).toBeNull();
    });

    test('parses local dataset with task filters', () => {
      const result = LocalDatasetConfigSchema.parse({
        kind: 'local' as const,
        path: '/data/datasets/test',
        task_names: ['task1', 'task2'],
        n_tasks: 5,
      });
      expect(result.task_names).toEqual(['task1', 'task2']);
      expect(result.n_tasks).toBe(5);
    });
  });

  describe('RegistryDatasetConfigSchema', () => {
    test('parses registry dataset with OSS registry (version stays null)', () => {
      const result = RegistryDatasetConfigSchema.parse({
        kind: 'registry' as const,
        name: 'test/dataset',
        registry: { split: 'train' },
      });
      expect(result.name).toBe('test/dataset');
      expect(result.version).toBeNull();
      expect(result.overwrite).toBe(false);
    });

    test('parses with explicit version', () => {
      const result = RegistryDatasetConfigSchema.parse({
        kind: 'registry' as const,
        name: 'test/dataset',
        registry: { split: 'train' },
        version: 'v2',
      });
      expect(result.version).toBe('v2');
    });
  });

  describe('DatasetConfigSchema (discriminated union)', () => {
    test('parses local dataset config', () => {
      const result = DatasetConfigSchema.parse({
        kind: 'local',
        path: '/data/datasets/test',
      });
      expect(result.kind).toBe('local');
      if (result.kind === 'local') {
        expect(result.path).toBe('/data/datasets/test');
      }
    });

    test('parses registry dataset config', () => {
      const result = DatasetConfigSchema.parse({
        kind: 'registry',
        name: 'test/dataset',
        registry: { split: 'train' },
      });
      expect(result.kind).toBe('registry');
      if (result.kind === 'registry') {
        expect(result.name).toBe('test/dataset');
      }
    });
  });

  describe('parseDatasetConfig (version inference)', () => {
    test('infers version from OSS registry split', () => {
      const result = parseDatasetConfig({
        kind: 'registry' as const,
        name: 'test/dataset',
        registry: { split: 'train' },
      });
      expect(result.kind).toBe('registry');
      if (result.kind === 'registry') {
        expect(result.version).toBe('train');
      }
    });

    test('infers version with revision', () => {
      const result = parseDatasetConfig({
        kind: 'registry' as const,
        name: 'test/dataset',
        registry: { split: 'train', revision: 'abc123' },
      });
      if (result.kind === 'registry') {
        expect(result.version).toBe('train@abc123');
      }
    });

    test('explicit version overrides inference', () => {
      const result = parseDatasetConfig({
        kind: 'registry' as const,
        name: 'test/dataset',
        registry: { split: 'train' },
        version: 'v2',
      });
      if (result.kind === 'registry') {
        expect(result.version).toBe('v2');
      }
    });

    test('no version inference for non-OSS registry', () => {
      const result = parseDatasetConfig({
        kind: 'registry' as const,
        name: 'test/dataset',
        registry: { url: 'https://example.com/registry.json' },
      });
      if (result.kind === 'registry') {
        expect(result.version).toBeNull();
      }
    });
  });
});

// ---------------------------------------------------------------------------
// HarborJobConfig
// ---------------------------------------------------------------------------
describe('HarborJobConfig', () => {
  describe('HarborJobConfigSchema', () => {
    test('parses minimal config with experiment_id only', () => {
      const result = HarborJobConfigSchema.parse({
        experiment_id: 'test-experiment',
      });
      // Base fields
      expect(result.experiment_id).toBe('test-experiment');
      expect(result.job_name).not.toBeNull(); // auto-generated
      expect(result.namespace).toBeNull();
      expect(result.timeout).toBe(7200); // default, may be auto-computed
      expect(result.labels).toEqual({});
      // Harbor native fields
      expect(result.jobs_dir).toBe('/data/logs/user-defined/jobs');
      expect(result.n_attempts).toBe(1);
      expect(result.timeout_multiplier).toBe(1.0);
      expect(result.debug).toBe(false);
      expect(result.agents).toHaveLength(1);
      expect(result.datasets).toEqual([]);
      expect(result.tasks).toEqual([]);
      expect(result.artifacts).toEqual([]);
      // Nested defaults
      expect(result.environment.image).toBe('python:3.11');
      expect(result.orchestrator.type).toBe(OrchestratorType.LOCAL);
      expect(result.verifier.disable).toBe(false);
      expect(result.metrics).toEqual([]);
    });

    test('parses full job config', () => {
      const result = HarborJobConfigSchema.parse({
        experiment_id: 'exp-001',
        job_name: 'my-job',
        namespace: 'ns-1',
        jobs_dir: '/custom/jobs',
        n_attempts: 3,
        timeout_multiplier: 2.0,
        debug: true,
        agents: [
          { name: 'test-agent', max_timeout_sec: 300 },
        ],
        datasets: [
          {
            kind: 'registry',
            name: 'test/dataset',
            registry: { split: 'train' },
          },
        ],
        tasks: [{ path: '/tasks/test' }],
        artifacts: ['/data/output', { source: '/data/output2', destination: '/results' }],
        labels: { env: 'test' },
      });
      expect(result.experiment_id).toBe('exp-001');
      expect(result.job_name).toBe('my-job');
      expect(result.namespace).toBe('ns-1');
      expect(result.n_attempts).toBe(3);
      expect(result.timeout_multiplier).toBe(2.0);
      expect(result.debug).toBe(true);
      expect(result.agents).toHaveLength(1);
      expect(result.agents[0]?.name).toBe('test-agent');
      expect(result.datasets).toHaveLength(1);
      expect(result.tasks).toHaveLength(1);
      expect(result.artifacts).toHaveLength(2);
      expect(result.labels).toEqual({ env: 'test' });
    });

    test('requires experiment_id (non-empty)', () => {
      expect(() =>
        HarborJobConfigSchema.parse({})
      ).toThrow(z.ZodError);
      expect(() =>
        HarborJobConfigSchema.parse({ experiment_id: '' })
      ).toThrow(z.ZodError);
    });

    test('auto-generates job_name when not provided', () => {
      const result = HarborJobConfigSchema.parse({
        experiment_id: 'test-experiment',
        datasets: [
          {
            kind: 'registry',
            name: 'org/dataset-name',
            registry: { split: 'train' },
            task_names: ['task-a'],
          },
        ],
      });
      // Format: {dataset_name}_{task_name}_{uuid8}
      expect(result.job_name).toMatch(/^dataset-name_task-a_[a-f0-9]{8}$/);
    });

    test('auto-generates job_name from first dataset only', () => {
      const result = HarborJobConfigSchema.parse({
        experiment_id: 'test-experiment',
        datasets: [
          { kind: 'local', path: '/data/datasets/my-dataset' },
        ],
      });
      // Local dataset has no name, just falls back to UUID8
      expect(result.job_name).toMatch(/^[a-f0-9]{8}$/);
    });

    test('validates job_name has no slash', () => {
      expect(() =>
        HarborJobConfigSchema.parse({
          experiment_id: 'test-experiment',
          job_name: 'invalid/name',
        })
      ).toThrow(z.ZodError);
    });

    // Validators
    test('syncs experiment_id to environment', () => {
      const result = HarborJobConfigSchema.parse({
        experiment_id: 'test-experiment',
      });
      expect(result.environment.experiment_id).toBe('test-experiment');
    });

    test('errors on experiment_id mismatch with environment', () => {
      expect(() =>
        HarborJobConfigSchema.parse({
          experiment_id: 'test-experiment',
          environment: {
            experiment_id: 'different-experiment',
          },
        })
      ).toThrow(z.ZodError);
    });

    test('computes timeout from agent config when default', () => {
      const result = HarborJobConfigSchema.parse({
        experiment_id: 'test-experiment',
        agents: [{ max_timeout_sec: 600 }],
        timeout_multiplier: 2.0,
      });
      // effective = int(600 * 2.0) + 600 = 1800
      expect(result.timeout).toBe(1800);
    });

    test('respects explicit timeout when set', () => {
      const result = HarborJobConfigSchema.parse({
        experiment_id: 'test-experiment',
        timeout: 3600, // explicitly set, not default
      });
      expect(result.timeout).toBe(3600);
    });
  });

  describe('createHarborJobConfig', () => {
    test('creates with experiment_id', () => {
      const result = createHarborJobConfig({
        experiment_id: 'test-experiment',
        job_name: 'manual-job',
      });
      expect(result.experiment_id).toBe('test-experiment');
      expect(result.job_name).toBe('manual-job');
    });

    test('auto-generates job_name when omitted', () => {
      const result = createHarborJobConfig({
        experiment_id: 'test-experiment',
      });
      expect(result.job_name).not.toBeNull();
      expect(typeof result.job_name).toBe('string');
      expect((result.job_name as string).length).toBeGreaterThan(0);
    });
  });
});
