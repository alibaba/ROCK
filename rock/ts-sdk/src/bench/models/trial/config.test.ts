import { z } from 'zod';
import {
  AgentConfig,
  AgentConfigSchema,
  createAgentConfig,
  EnvironmentConfig,
  EnvironmentConfigSchema,
  createEnvironmentConfig,
  TemplateConfigSchema,
  NativeConfigSchema,
  VerifierConfig,
  VerifierConfigSchema,
  createVerifierConfig,
  TaskConfig,
  TaskConfigSchema,
  createTaskConfig,
  ArtifactConfig,
  ArtifactConfigSchema,
  createArtifactConfig,
  RockEnvironmentConfigSchema,
  toHarborEnvironment,
} from './config';
import { EnvironmentType } from '../environment_type';

// ---------------------------------------------------------------------------
// AgentConfig
// ---------------------------------------------------------------------------
describe('AgentConfig', () => {
  describe('AgentConfigSchema', () => {
    test('parses empty object with defaults', () => {
      const result = AgentConfigSchema.parse({});
      expect(result.name).toBeNull();
      expect(result.import_path).toBeNull();
      expect(result.model_name).toBeNull();
      expect(result.override_timeout_sec).toBeNull();
      expect(result.override_setup_timeout_sec).toBeNull();
      expect(result.max_timeout_sec).toBeNull();
      expect(result.kwargs).toEqual({});
      expect(result.env).toEqual({});
    });

    test('parses fully specified agent config', () => {
      const result = AgentConfigSchema.parse({
        name: 'test-agent',
        import_path: 'agents.test',
        model_name: 'gpt-4',
        override_timeout_sec: 300,
        max_timeout_sec: 600,
        kwargs: { temperature: 0.7 },
        env: { OPENAI_API_KEY: 'test' },
      });
      expect(result.name).toBe('test-agent');
      expect(result.import_path).toBe('agents.test');
      expect(result.model_name).toBe('gpt-4');
      expect(result.override_timeout_sec).toBe(300);
      expect(result.max_timeout_sec).toBe(600);
      expect(result.kwargs).toEqual({ temperature: 0.7 });
      expect(result.env).toEqual({ OPENAI_API_KEY: 'test' });
    });
  });

  describe('createAgentConfig', () => {
    test('creates with defaults', () => {
      const result = createAgentConfig();
      expect(result.name).toBeNull();
      expect(result.kwargs).toEqual({});
    });

    test('creates with partial overrides', () => {
      const result = createAgentConfig({ name: 'test-agent' });
      expect(result.name).toBe('test-agent');
    });
  });
});

// ---------------------------------------------------------------------------
// EnvironmentConfig (Harbor-level)
// ---------------------------------------------------------------------------
describe('EnvironmentConfig', () => {
  describe('EnvironmentConfigSchema', () => {
    test('parses empty object with defaults', () => {
      const result = EnvironmentConfigSchema.parse({});
      expect(result.type).toBeNull();
      expect(result.import_path).toBeNull();
      expect(result.force_build).toBe(false);
      expect(result.delete).toBe(true);
      expect(result.override_cpus).toBeNull();
      expect(result.override_memory_mb).toBeNull();
      expect(result.override_storage_mb).toBeNull();
      expect(result.override_gpus).toBeNull();
      expect(result.suppress_override_warnings).toBe(false);
      expect(result.mounts_json).toBeNull();
      expect(result.oss_mirror).toBeNull();
      expect(result.tracking).toBeNull();
      expect(result.oss_deps).toEqual({});
      expect(result.env).toEqual({});
      expect(result.kwargs).toEqual({});
    });

    test('parses environment type', () => {
      const result = EnvironmentConfigSchema.parse({
        type: EnvironmentType.DOCKER,
      });
      expect(result.type).toBe(EnvironmentType.DOCKER);
    });

    test('parses resource overrides', () => {
      const result = EnvironmentConfigSchema.parse({
        override_cpus: 4,
        override_memory_mb: 16384,
        override_storage_mb: 50000,
        override_gpus: 1,
      });
      expect(result.override_cpus).toBe(4);
      expect(result.override_memory_mb).toBe(16384);
      expect(result.override_storage_mb).toBe(50000);
      expect(result.override_gpus).toBe(1);
    });
  });

  describe('createEnvironmentConfig', () => {
    test('creates with defaults', () => {
      const result = createEnvironmentConfig();
      expect(result.force_build).toBe(false);
      expect(result.delete).toBe(true);
    });
  });
});

// ---------------------------------------------------------------------------
// VerifierConfig
// ---------------------------------------------------------------------------
describe('VerifierConfig', () => {
  describe('VerifierConfigSchema', () => {
    test('parses empty object with defaults', () => {
      const result = VerifierConfigSchema.parse({});
      expect(result.override_timeout_sec).toBeNull();
      expect(result.max_timeout_sec).toBeNull();
      expect(result.disable).toBe(false);
      expect(result.patch).toBeNull();
      expect(result.mode).toBeNull();
      expect(result.env).toEqual({});
      expect(result.native_config).toEqual({
        image: null,
        script: null,
        oss_deps: {},
        template: null,
      });
    });

    test('parses mode and native config', () => {
      const result = VerifierConfigSchema.parse({
        mode: 'native',
        native_config: {
          image: 'ubuntu:latest',
          script: 'bash run.sh',
        },
      });
      expect(result.mode).toBe('native');
      expect(result.native_config.image).toBe('ubuntu:latest');
      expect(result.native_config.script).toBe('bash run.sh');
    });

    test('parses verifier env', () => {
      const result = VerifierConfigSchema.parse({
        env: {
          OPENAI_API_KEY: '${OPENAI_API_KEY}',
          JUDGE_MODEL: 'openai/gpt-5',
        },
      });
      expect(result.env).toEqual({
        OPENAI_API_KEY: '${OPENAI_API_KEY}',
        JUDGE_MODEL: 'openai/gpt-5',
      });
    });

    test('rejects invalid mode', () => {
      expect(() =>
        VerifierConfigSchema.parse({ mode: 'invalid' })
      ).toThrow(z.ZodError);
    });
  });

  describe('createVerifierConfig', () => {
    test('creates with defaults', () => {
      const result = createVerifierConfig();
      expect(result.disable).toBe(false);
    });
  });
});

// ---------------------------------------------------------------------------
// NativeConfig / TemplateConfig
// ---------------------------------------------------------------------------
describe('NativeConfig', () => {
  test('parses with template', () => {
    const result = NativeConfigSchema.parse({
      image: 'ubuntu:latest',
      template: { name: 'template-v1', revision: 'abc123' },
    });
    expect(result.image).toBe('ubuntu:latest');
    expect(result.template).toEqual({ name: 'template-v1', revision: 'abc123' });
  });

  describe('TemplateConfig', () => {
    test('parses with name and revision', () => {
      const result = TemplateConfigSchema.parse({
        name: 'template-v1',
        revision: 'abc123',
      });
      expect(result.name).toBe('template-v1');
      expect(result.revision).toBe('abc123');
    });
  });
});

// ---------------------------------------------------------------------------
// TaskConfig
// ---------------------------------------------------------------------------
describe('TaskConfig', () => {
  describe('TaskConfigSchema', () => {
    test('parses minimal task config (path only)', () => {
      const result = TaskConfigSchema.parse({
        path: '/data/tasks/my-task',
      });
      expect(result.path).toBe('/data/tasks/my-task');
      expect(result.git_url).toBeNull();
      expect(result.git_commit_id).toBeNull();
      expect(result.overwrite).toBe(false);
      expect(result.download_dir).toBeNull();
      expect(result.source).toBeNull();
    });

    test('parses full task config', () => {
      const result = TaskConfigSchema.parse({
        path: '/data/tasks/my-task',
        git_url: 'https://github.com/example/repo.git',
        git_commit_id: 'abc123',
        overwrite: true,
        download_dir: '/tmp/downloads',
        source: 'github',
      });
      expect(result.git_url).toBe('https://github.com/example/repo.git');
      expect(result.git_commit_id).toBe('abc123');
      expect(result.overwrite).toBe(true);
      expect(result.download_dir).toBe('/tmp/downloads');
      expect(result.source).toBe('github');
    });

    test('requires path field', () => {
      expect(() => TaskConfigSchema.parse({})).toThrow(z.ZodError);
    });
  });

  describe('createTaskConfig', () => {
    test('creates with path', () => {
      const result = createTaskConfig({ path: '/data/tasks/test' });
      expect(result.path).toBe('/data/tasks/test');
    });
  });
});

// ---------------------------------------------------------------------------
// ArtifactConfig
// ---------------------------------------------------------------------------
describe('ArtifactConfig', () => {
  describe('ArtifactConfigSchema', () => {
    test('parses minimal artifact', () => {
      const result = ArtifactConfigSchema.parse({ source: '/data/output' });
      expect(result.source).toBe('/data/output');
      expect(result.destination).toBeNull();
    });

    test('parses artifact with destination', () => {
      const result = ArtifactConfigSchema.parse({
        source: '/data/output',
        destination: '/results',
      });
      expect(result.source).toBe('/data/output');
      expect(result.destination).toBe('/results');
    });

    test('requires source field', () => {
      expect(() => ArtifactConfigSchema.parse({})).toThrow(z.ZodError);
    });
  });

  describe('createArtifactConfig', () => {
    test('creates with source', () => {
      const result = createArtifactConfig({ source: '/data/output' });
      expect(result.source).toBe('/data/output');
    });
  });
});

// ---------------------------------------------------------------------------
// RockEnvironmentConfig (combined Sandbox + Harbor environment)
// ---------------------------------------------------------------------------
describe('RockEnvironmentConfig', () => {
  describe('RockEnvironmentConfigSchema', () => {
    test('parses empty object with sandbox defaults', () => {
      const result = RockEnvironmentConfigSchema.parse({});
      // SandboxConfig defaults
      expect(result.image).toBe('python:3.11');
      expect(result.memory).toBe('8g');
      expect(result.cpus).toBe(2);
      // Harbor EnvironmentConfig defaults
      expect(result.force_build).toBe(false);
      expect(result.delete).toBe(true);
      // envhub-level defaults
      expect(result.uploads).toEqual([]);
      expect(result.env).toEqual({});
      expect(result.oss_mirror).toBeNull();
      expect(result.proxy).toBeNull();
      expect(result.tracking).toBeNull();
    });

    test('parses sandbox-specific fields', () => {
      const result = RockEnvironmentConfigSchema.parse({
        image: 'ubuntu:22.04',
        auto_clear_seconds: 600,
        startup_timeout: 300,
        memory: '16g',
        cpus: 4,
        user_id: 'user-123',
        experiment_id: 'exp-456',
        cluster: 'prod',
        namespace: 'ns-test',
      });
      expect(result.image).toBe('ubuntu:22.04');
      expect(result.auto_clear_seconds).toBe(600);
      expect(result.startup_timeout).toBe(300);
      expect(result.memory).toBe('16g');
      expect(result.cpus).toBe(4);
      expect(result.user_id).toBe('user-123');
      expect(result.experiment_id).toBe('exp-456');
      expect(result.cluster).toBe('prod');
      expect(result.namespace).toBe('ns-test');
    });

    test('parses harbor environment fields', () => {
      const result = RockEnvironmentConfigSchema.parse({
        force_build: true,
        override_cpus: 8,
        override_memory_mb: 32768,
        oss_deps: { 'package.tar.gz': 'oss://bucket/key' },
      });
      expect(result.force_build).toBe(true);
      expect(result.override_cpus).toBe(8);
      expect(result.override_memory_mb).toBe(32768);
      expect(result.oss_deps).toEqual({ 'package.tar.gz': 'oss://bucket/key' });
    });

    test('parses envhub-level uploads', () => {
      const result = RockEnvironmentConfigSchema.parse({
        uploads: [
          ['/local/file.txt', '/sandbox/file.txt'],
          ['/local/dir', '/sandbox/dir'],
        ],
      });
      expect(result.uploads).toEqual([
        ['/local/file.txt', '/sandbox/file.txt'],
        ['/local/dir', '/sandbox/dir'],
      ]);
    });
  });

  describe('toHarborEnvironment', () => {
    test('strips Rock-only sandbox fields', () => {
      const config = RockEnvironmentConfigSchema.parse({
        image: 'ubuntu:22.04',
        memory: '16g',
        cpus: 4,
        force_build: true,
        override_cpus: 8,
        type: EnvironmentType.DOCKER,
      });
      const harbor = toHarborEnvironment(config);
      // Sandbox fields are stripped
      expect(harbor).not.toHaveProperty('image');
      expect(harbor).not.toHaveProperty('memory');
      expect(harbor).not.toHaveProperty('cpus');
      // Harbor fields are kept
      expect(harbor.force_build).toBe(true);
      expect(harbor.override_cpus).toBe(8);
      expect(harbor.type).toBe(EnvironmentType.DOCKER);
    });
  });
});
