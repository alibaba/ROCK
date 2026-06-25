/**
 * Tests for job/config_compose.ts — ComposeJobConfig and sub-models
 */

import { z } from 'zod';

// Environment schema (same minimal version as config.test.ts)
import { SandboxConfigSchema } from '../sandbox/config';
const EnvironmentConfigSchema = SandboxConfigSchema.extend({
  uploads: z.array(z.tuple([z.string(), z.string()])).default([]),
  env: z.record(z.string()).default({}),
  oss_mirror: z.any().nullable().default(null),
  proxy: z.any().nullable().default(null),
  tracking: z.any().nullable().default(null),
});

import {
  ResourceConfigSchema,
  ResourceConfig,
  VolumeMountSchema,
  VolumeMount,
  VolumeConfigSchema,
  VolumeConfig,
  ServiceConfigSchema,
  ServiceConfig,
  InitContainerConfigSchema,
  InitContainerConfig,
  OSSArtifactConfigSchema,
  OSSArtifactConfig,
  ComposeJobConfigSchema,
  ComposeJobConfig,
} from './config_compose';

// ---------------------------------------------------------------------------
// ResourceConfig
// ---------------------------------------------------------------------------
describe('ResourceConfig', () => {
  describe('ResourceConfigSchema', () => {
    test('parses empty object with defaults', () => {
      const result = ResourceConfigSchema.parse({});
      expect(result.cpu).toBe('1');
      expect(result.memory).toBe('2Gi');
    });

    test('parses custom resources', () => {
      const result = ResourceConfigSchema.parse({ cpu: '2', memory: '4Gi' });
      expect(result.cpu).toBe('2');
      expect(result.memory).toBe('4Gi');
    });

    test('accepts numeric cpu', () => {
      const result = ResourceConfigSchema.parse({ cpu: 2.5, memory: '8Gi' });
      expect(result.cpu).toBe(2.5);
    });
  });
});

// ---------------------------------------------------------------------------
// VolumeMount
// ---------------------------------------------------------------------------
describe('VolumeMount', () => {
  describe('VolumeMountSchema', () => {
    test('parses with required fields', () => {
      const result = VolumeMountSchema.parse({ name: 'data', mount_path: '/data' });
      expect(result.name).toBe('data');
      expect(result.mount_path).toBe('/data');
      expect(result.read_only).toBe(false);
    });

    test('rejects missing required fields', () => {
      expect(() => VolumeMountSchema.parse({})).toThrow();
    });

    test('parses read-only mount', () => {
      const result = VolumeMountSchema.parse({ name: 'config', mount_path: '/etc/config', read_only: true });
      expect(result.read_only).toBe(true);
    });
  });
});

// ---------------------------------------------------------------------------
// VolumeConfig
// ---------------------------------------------------------------------------
describe('VolumeConfig', () => {
  describe('VolumeConfigSchema', () => {
    test('parses named volume', () => {
      const result = VolumeConfigSchema.parse({ name: 'shared' });
      expect(result.name).toBe('shared');
      expect(result.host_path).toBeNull();
    });

    test('parses host path volume', () => {
      const result = VolumeConfigSchema.parse({ name: 'data', host_path: '/host/data' });
      expect(result.name).toBe('data');
      expect(result.host_path).toBe('/host/data');
    });
  });
});

// ---------------------------------------------------------------------------
// ServiceConfig
// ---------------------------------------------------------------------------
describe('ServiceConfig', () => {
  describe('ServiceConfigSchema', () => {
    test('parses minimal service', () => {
      const result = ServiceConfigSchema.parse({ name: 'web', image: 'nginx:latest' });
      expect(result.name).toBe('web');
      expect(result.image).toBe('nginx:latest');
      expect(result.command).toBeNull();
      expect(result.args).toBeNull();
      expect(result.script).toBeNull();
      expect(result.env).toEqual({});
      expect(result.ports).toEqual([]);
      expect(result.resources).toBeNull();
      expect(result.privileged).toBe(false);
      expect(result.volume_mounts).toEqual([]);
      expect(result.is_main).toBe(false);
    });

    test('parses full service config', () => {
      const result = ServiceConfigSchema.parse({
        name: 'main',
        image: 'myapp:latest',
        command: ['bash', '-c'],
        args: ['echo hello'],
        script: '#!/bin/bash\necho done',
        env: { FOO: 'bar' },
        ports: [8080],
        resources: { cpu: '4', memory: '8Gi' },
        privileged: true,
        volume_mounts: [{ name: 'data', mount_path: '/data', read_only: true }],
        is_main: true,
      });
      expect(result.name).toBe('main');
      expect(result.command).toEqual(['bash', '-c']);
      expect(result.args).toEqual(['echo hello']);
      expect(result.script).toBe('#!/bin/bash\necho done');
      expect(result.env).toEqual({ FOO: 'bar' });
      expect(result.ports).toEqual([8080]);
      expect(result.resources?.cpu).toBe('4');
      expect(result.privileged).toBe(true);
      expect(result.volume_mounts).toHaveLength(1);
      expect(result.is_main).toBe(true);
    });
  });
});

// ---------------------------------------------------------------------------
// InitContainerConfig
// ---------------------------------------------------------------------------
describe('InitContainerConfig', () => {
  describe('InitContainerConfigSchema', () => {
    test('parses minimal init container', () => {
      const result = InitContainerConfigSchema.parse({ name: 'init', image: 'alpine' });
      expect(result.name).toBe('init');
      expect(result.image).toBe('alpine');
      expect(result.command).toBeNull();
      expect(result.args).toBeNull();
      expect(result.script).toBeNull();
      expect(result.volume_mounts).toEqual([]);
    });

    test('parses init container with script', () => {
      const result = InitContainerConfigSchema.parse({
        name: 'setup',
        image: 'alpine',
        script: 'apk add curl',
      });
      expect(result.script).toBe('apk add curl');
    });

    test('parses init container with command and args', () => {
      const result = InitContainerConfigSchema.parse({
        name: 'migrate',
        image: 'migrator:latest',
        command: ['python', 'migrate.py'],
        args: ['--force'],
      });
      expect(result.command).toEqual(['python', 'migrate.py']);
      expect(result.args).toEqual(['--force']);
    });
  });
});

// ---------------------------------------------------------------------------
// OSSArtifactConfig
// ---------------------------------------------------------------------------
describe('OSSArtifactConfig', () => {
  describe('OSSArtifactConfigSchema', () => {
    test('parses with required fields', () => {
      const result = OSSArtifactConfigSchema.parse({ name: 'model', oss_key: 'models/v1' });
      expect(result.name).toBe('model');
      expect(result.oss_key).toBe('models/v1');
      expect(result.target_path).toBe('/tmp/shared');
      expect(result.archive).toBe(true);
    });

    test('parses with custom target and no archive', () => {
      const result = OSSArtifactConfigSchema.parse({
        name: 'data',
        oss_key: 'data/dataset',
        target_path: '/workspace',
        archive: false,
      });
      expect(result.target_path).toBe('/workspace');
      expect(result.archive).toBe(false);
    });
  });
});

// ---------------------------------------------------------------------------
// ComposeJobConfig
// ---------------------------------------------------------------------------
describe('ComposeJobConfig', () => {
  const schema = ComposeJobConfigSchema(EnvironmentConfigSchema);

  describe('ComposeJobConfigSchema', () => {
    test('parses with single main service', () => {
      const result = schema.parse({
        services: [
          { name: 'main', image: 'myapp:latest', is_main: true },
        ],
      });
      expect(result.services).toHaveLength(1);
      expect(result.services[0]!.is_main).toBe(true);
    });

    test('validates exactly one is_main service', () => {
      // No is_main service
      expect(() =>
        schema.parse({
          services: [
            { name: 'a', image: 'img1' },
            { name: 'b', image: 'img2' },
          ],
        })
      ).toThrow();

      // Multiple is_main services
      expect(() =>
        schema.parse({
          services: [
            { name: 'a', image: 'img1', is_main: true },
            { name: 'b', image: 'img2', is_main: true },
          ],
        })
      ).toThrow();
    });

    test('parses with all optional fields', () => {
      const result = schema.parse({
        services: [
          { name: 'main', image: 'myapp', is_main: true },
          { name: 'db', image: 'postgres', ports: [5432] },
        ],
        init_containers: [
          { name: 'setup', image: 'alpine', script: 'echo init' },
        ],
        volumes: [
          { name: 'data', host_path: '/host/data' },
        ],
        oss_artifacts: [
          { name: 'model', oss_key: 'models/v1.tar.gz' },
        ],
        network_mode: 'bridge',
        callback_url: 'http://example.com/callback',
        job_name: 'compose-job',
        experiment_id: 'exp-1',
      });
      expect(result.services).toHaveLength(2);
      expect(result.init_containers).toHaveLength(1);
      expect(result.volumes).toHaveLength(1);
      expect(result.oss_artifacts).toHaveLength(1);
      expect(result.network_mode).toBe('bridge');
      expect(result.callback_url).toBe('http://example.com/callback');
    });

    test('defaults network_mode to host', () => {
      const result = schema.parse({
        services: [{ name: 'main', image: 'img', is_main: true }],
      });
      expect(result.network_mode).toBe('host');
    });

    test('defaults empty lists for optional arrays', () => {
      const result = schema.parse({
        services: [{ name: 'main', image: 'img', is_main: true }],
      });
      expect(result.init_containers).toEqual([]);
      expect(result.volumes).toEqual([]);
      expect(result.oss_artifacts).toEqual([]);
    });

    test('inherits base JobConfig fields', () => {
      const result = schema.parse({
        services: [{ name: 'main', image: 'img', is_main: true }],
        namespace: 'ns-1',
        labels: { tier: 'prod' },
        timeout: 14400,
      });
      expect(result.namespace).toBe('ns-1');
      expect(result.labels).toEqual({ tier: 'prod' });
      expect(result.timeout).toBe(14400);
    });

    test('rejects extra unknown fields', () => {
      expect(() =>
        schema.parse({
          services: [{ name: 'main', image: 'img', is_main: true }],
          unknown_field: 'nope',
        })
      ).toThrow();
    });
  });
});
