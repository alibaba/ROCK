/**
 * Tests for Datasets models (Zod schemas)
 */

import {
  DatasetSpecSchema,
  UploadResultSchema,
  OssRegistryInfoSchema,
  LocalDatasetConfigSchema,
  RegistryDatasetConfigSchema,
  OssMirrorConfigSchema,
} from './models.js';

// ---------------------------------------------------------------------------
// DatasetSpec
// ---------------------------------------------------------------------------

describe('DatasetSpecSchema', () => {
  test('should parse valid data with taskIds', () => {
    const spec = DatasetSpecSchema.parse({
      id: 'princeton-nlp/SWE-bench_Verified',
      split: 'test',
      taskIds: ['task-001', 'task-002'],
    });

    expect(spec.id).toBe('princeton-nlp/SWE-bench_Verified');
    expect(spec.split).toBe('test');
    expect(spec.taskIds).toEqual(['task-001', 'task-002']);
  });

  test('should default taskIds to empty array', () => {
    const spec = DatasetSpecSchema.parse({
      id: 'org/dataset',
      split: 'train',
    });

    expect(spec.taskIds).toEqual([]);
  });

  test('should reject missing required fields', () => {
    expect(() => DatasetSpecSchema.parse({})).toThrow();
    expect(() => DatasetSpecSchema.parse({ id: 'x' })).toThrow();
    expect(() => DatasetSpecSchema.parse({ split: 'y' })).toThrow();
  });

  test('should reject non-string taskIds', () => {
    expect(() =>
      DatasetSpecSchema.parse({
        id: 'org/dataset',
        split: 'test',
        taskIds: [1, 2, 3],
      })
    ).toThrow();
  });
});

// ---------------------------------------------------------------------------
// UploadResult
// ---------------------------------------------------------------------------

describe('UploadResultSchema', () => {
  test('should parse valid upload result', () => {
    const result = UploadResultSchema.parse({
      id: 'org/dataset',
      split: 'test',
      uploaded: 42,
      skipped: 3,
      failed: 1,
    });

    expect(result.id).toBe('org/dataset');
    expect(result.split).toBe('test');
    expect(result.uploaded).toBe(42);
    expect(result.skipped).toBe(3);
    expect(result.failed).toBe(1);
  });

  test('should reject negative counts', () => {
    expect(() =>
      UploadResultSchema.parse({
        id: 'org/dataset',
        split: 'test',
        uploaded: -1,
        skipped: 0,
        failed: 0,
      })
    ).toThrow();
  });

  test('should reject missing fields', () => {
    expect(() => UploadResultSchema.parse({ id: 'x' })).toThrow();
    expect(() =>
      UploadResultSchema.parse({ id: 'x', split: 'y', uploaded: 0 })
    ).toThrow();
  });

  test('should allow zero for all counts', () => {
    const result = UploadResultSchema.parse({
      id: 'org/dataset',
      split: 'train',
      uploaded: 0,
      skipped: 0,
      failed: 0,
    });

    expect(result.uploaded).toBe(0);
    expect(result.skipped).toBe(0);
    expect(result.failed).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// OssRegistryInfo
// ---------------------------------------------------------------------------

describe('OssRegistryInfoSchema', () => {
  test('should parse empty object with all defaults', () => {
    const info = OssRegistryInfoSchema.parse({});
    expect(info.split).toBeNull();
    expect(info.revision).toBeNull();
    expect(info.ossDatasetPath).toBeNull();
    expect(info.ossAccessKeyId).toBeNull();
    expect(info.ossAccessKeySecret).toBeNull();
    expect(info.ossRegion).toBeNull();
    expect(info.ossEndpoint).toBeNull();
    expect(info.ossBucket).toBeNull();
  });

  test('should parse with all fields set', () => {
    const info = OssRegistryInfoSchema.parse({
      split: 'test',
      revision: 'v1',
      ossDatasetPath: 'datasets',
      ossAccessKeyId: 'key-id',
      ossAccessKeySecret: 'secret',
      ossRegion: 'cn-hangzhou',
      ossEndpoint: 'https://oss-cn-hangzhou.aliyuncs.com',
      ossBucket: 'my-bucket',
    });

    expect(info.split).toBe('test');
    expect(info.revision).toBe('v1');
    expect(info.ossAccessKeyId).toBe('key-id');
    expect(info.ossEndpoint).toBe('https://oss-cn-hangzhou.aliyuncs.com');
  });
});

// ---------------------------------------------------------------------------
// LocalDatasetConfig
// ---------------------------------------------------------------------------

describe('LocalDatasetConfigSchema', () => {
  test('should parse with required path', () => {
    const config = LocalDatasetConfigSchema.parse({
      path: '/data/datasets/swE-bench',
    });

    expect(config.path).toBe('/data/datasets/swE-bench');
    expect(config.taskNames).toBeNull();
    expect(config.excludeTaskNames).toBeNull();
    expect(config.nTasks).toBeNull();
  });

  test('should parse with optional fields', () => {
    const config = LocalDatasetConfigSchema.parse({
      path: '/data/ds',
      taskNames: ['task-1', 'task-2'],
      excludeTaskNames: ['task-3'],
      nTasks: 10,
    });

    expect(config.taskNames).toEqual(['task-1', 'task-2']);
    expect(config.excludeTaskNames).toEqual(['task-3']);
    expect(config.nTasks).toBe(10);
  });

  test('should reject missing path', () => {
    expect(() => LocalDatasetConfigSchema.parse({})).toThrow();
  });
});

// ---------------------------------------------------------------------------
// RegistryDatasetConfig
// ---------------------------------------------------------------------------

describe('RegistryDatasetConfigSchema', () => {
  test('should parse with minimal fields', () => {
    const config = RegistryDatasetConfigSchema.parse({
      name: 'princeton-nlp/SWE-bench_Verified',
      registry: {},
    });

    expect(config.name).toBe('princeton-nlp/SWE-bench_Verified');
    expect(config.registry).toBeDefined();
    expect(config.version).toBeNull();
    expect(config.overwrite).toBe(false);
  });

  test('should parse with all fields', () => {
    const config = RegistryDatasetConfigSchema.parse({
      name: 'org/dataset',
      registry: { split: 'test', ossBucket: 'bucket' },
      version: 'test@v1',
      overwrite: true,
      taskNames: ['task-1'],
      downloadDir: '/tmp/dl',
    });

    expect(config.version).toBe('test@v1');
    expect(config.overwrite).toBe(true);
    expect(config.downloadDir).toBe('/tmp/dl');
  });

  test('should reject missing name', () => {
    expect(() =>
      RegistryDatasetConfigSchema.parse({ registry: {} })
    ).toThrow();
  });

  test('should reject missing registry', () => {
    expect(() =>
      RegistryDatasetConfigSchema.parse({ name: 'org/ds' })
    ).toThrow();
  });
});

// ---------------------------------------------------------------------------
// OssMirrorConfig
// ---------------------------------------------------------------------------

describe('OssMirrorConfigSchema', () => {
  test('should parse with defaults', () => {
    const config = OssMirrorConfigSchema.parse({});
    expect(config.enabled).toBe(false);
    expect(config.ossBucket).toBeNull();
    expect(config.namespace).toBeNull();
    expect(config.experimentId).toBeNull();
    expect(config.ossAccessKeyId).toBeNull();
    expect(config.ossAccessKeySecret).toBeNull();
    expect(config.ossRegion).toBeNull();
    expect(config.ossEndpoint).toBeNull();
  });

  test('should parse enabled config', () => {
    const config = OssMirrorConfigSchema.parse({
      enabled: true,
      ossBucket: 'my-bucket',
      ossAccessKeyId: 'key',
      ossAccessKeySecret: 'secret',
      ossRegion: 'cn-hangzhou',
      ossEndpoint: 'https://oss-cn-hangzhou.aliyuncs.com',
    });

    expect(config.enabled).toBe(true);
    expect(config.ossBucket).toBe('my-bucket');
  });
});
