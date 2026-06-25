/**
 * Tests for OssDatasetRegistry
 *
 * Uses a mock OSS client to avoid real network calls.
 */

import { OssDatasetRegistry } from './oss.js';
import type { OssRegistryInfo, LocalDatasetConfig, RegistryDatasetConfig } from '../models.js';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

// ---------------------------------------------------------------------------
// Mock helpers
// ---------------------------------------------------------------------------

interface MockObject {
  name: string;
  size: number;
  lastModified: string;
  etag: string;
  type: string;
  storageClass: string;
}

interface MockListResult {
  objects: MockObject[];
  prefixes: string[];
  isTruncated: boolean;
  nextContinuationToken: string;
  keyCount: number;
  res: { status: number; headers: Record<string, unknown>; size: number; rt: number };
}

/** Build a minimal OSS mock result with prefix list (directory markers) */
function makeMockResult(prefixes: string[]): MockListResult {
  return {
    objects: [],
    prefixes,
    isTruncated: false,
    nextContinuationToken: '',
    keyCount: prefixes.length,
    res: { status: 200, headers: {}, size: 0, rt: 10 },
  };
}

/** Build a mock result with object list */
function makeMockObjectResult(names: string[]): MockListResult {
  return {
    objects: names.map((name) => ({
      name,
      size: 100,
      lastModified: '2024-01-01T00:00:00.000Z',
      etag: '"abc"',
      type: 'Normal',
      storageClass: 'Standard',
    })),
    prefixes: [],
    isTruncated: false,
    nextContinuationToken: '',
    keyCount: names.length,
    res: { status: 200, headers: {}, size: 0, rt: 10 },
  };
}

function defaultRegistryInfo(overrides?: Partial<OssRegistryInfo>): OssRegistryInfo {
  return {
    split: null,
    revision: null,
    ossDatasetPath: null,
    ossAccessKeyId: 'test-key',
    ossAccessKeySecret: 'test-secret',
    ossRegion: null,
    ossEndpoint: 'https://oss-cn-hangzhou.aliyuncs.com',
    ossBucket: 'test-bucket',
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('OssDatasetRegistry', () => {
  // ---- prefix construction ----
  describe('_buildPrefix', () => {
    test('should use default base path "datasets"', () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo({ ossDatasetPath: null }));
      const prefix = (registry as any).buildPrefix('my-org', 'my-dataset');
      expect(prefix).toBe('datasets/my-org/my-dataset');
    });

    test('should use custom base path', () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo({ ossDatasetPath: 'custom-base' }));
      const prefix = (registry as any).buildPrefix('org', 'ds');
      expect(prefix).toBe('custom-base/org/ds');
    });

    test('should include split when provided', () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());
      const prefix = (registry as any).buildPrefix('org', 'ds', 'test');
      expect(prefix).toBe('datasets/org/ds/test');
    });
  });

  // ---- lastSegment ----
  describe('_lastSegment', () => {
    test('should extract last segment', () => {
      expect(OssDatasetRegistry.lastSegment('datasets/org/')).toBe('org');
      expect(OssDatasetRegistry.lastSegment('a/b/c/')).toBe('c');
      expect(OssDatasetRegistry.lastSegment('single')).toBe('single');
    });

    test('should handle trailing slash', () => {
      expect(OssDatasetRegistry.lastSegment('datasets/org/ds/')).toBe('ds');
    });

    test('should handle no trailing slash', () => {
      expect(OssDatasetRegistry.lastSegment('datasets/org/ds')).toBe('ds');
    });
  });

  // ---- _extractTasksFromSplit ----
  describe('_extractTasksFromSplit', () => {
    test('should extract tasks from directory prefixes', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());
      const mockListV2 = jest.fn().mockResolvedValue(
        makeMockResult(['datasets/org/ds/test/task-001/', 'datasets/org/ds/test/task-002/'])
      );
      (registry as any).buildBucket = jest.fn().mockReturnValue({ listV2: mockListV2 });

      const tasks = await (registry as any).extractTasksFromSplit(
        { listV2: mockListV2 },
        'datasets/org/ds/test/'
      );

      expect(tasks).toEqual(['task-001', 'task-002']);
    });

    test('should extract tasks from file objects', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());
      const result = makeMockObjectResult([
        'datasets/org/ds/test/task-001.json',
        'datasets/org/ds/test/task-002.json',
      ]);
      // Add prefixes also empty to isolate file-only case
      const mockListV2 = jest.fn().mockResolvedValue(result);
      (registry as any).buildBucket = jest.fn().mockReturnValue({ listV2: mockListV2 });

      const tasks = await (registry as any).extractTasksFromSplit(
        { listV2: mockListV2 },
        'datasets/org/ds/test/'
      );

      expect(tasks).toEqual(['task-001', 'task-002']);
    });

    test('should ignore placeholder objects ending with /', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());
      const result: MockListResult = {
        objects: [
          { name: 'datasets/org/ds/test/', size: 0, lastModified: '', etag: '', type: 'Normal', storageClass: 'Standard' },
          { name: 'datasets/org/ds/test/task-001.json', size: 100, lastModified: '', etag: '', type: 'Normal', storageClass: 'Standard' },
        ],
        prefixes: [],
        isTruncated: false,
        nextContinuationToken: '',
        keyCount: 2,
        res: { status: 200, headers: {}, size: 0, rt: 10 },
      };
      const mockListV2 = jest.fn().mockResolvedValue(result);
      (registry as any).buildBucket = jest.fn().mockReturnValue({ listV2: mockListV2 });

      const tasks = await (registry as any).extractTasksFromSplit(
        { listV2: mockListV2 },
        'datasets/org/ds/test/'
      );

      expect(tasks).toEqual(['task-001']);
    });

    test('should merge and deduplicate directory and file tasks', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());
      const result: MockListResult = {
        objects: [
          { name: 'datasets/org/ds/test/task-001.json', size: 100, lastModified: '', etag: '', type: 'Normal', storageClass: 'Standard' },
        ],
        prefixes: ['datasets/org/ds/test/task-001/', 'datasets/org/ds/test/task-002/'],
        isTruncated: false,
        nextContinuationToken: '',
        keyCount: 3,
        res: { status: 200, headers: {}, size: 0, rt: 10 },
      };
      const mockListV2 = jest.fn().mockResolvedValue(result);
      (registry as any).buildBucket = jest.fn().mockReturnValue({ listV2: mockListV2 });

      const tasks = await (registry as any).extractTasksFromSplit(
        { listV2: mockListV2 },
        'datasets/org/ds/test/'
      );

      expect(tasks).toEqual(['task-001', 'task-002']);
    });

    test('should ignore nested paths', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());
      const result: MockListResult = {
        objects: [
          { name: 'datasets/org/ds/test/task-001.json', size: 100, lastModified: '', etag: '', type: 'Normal', storageClass: 'Standard' },
          { name: 'datasets/org/ds/test/subdir/nested.json', size: 100, lastModified: '', etag: '', type: 'Normal', storageClass: 'Standard' },
        ],
        prefixes: [],
        isTruncated: false,
        nextContinuationToken: '',
        keyCount: 2,
        res: { status: 200, headers: {}, size: 0, rt: 10 },
      };
      const mockListV2 = jest.fn().mockResolvedValue(result);
      (registry as any).buildBucket = jest.fn().mockReturnValue({ listV2: mockListV2 });

      const tasks = await (registry as any).extractTasksFromSplit(
        { listV2: mockListV2 },
        'datasets/org/ds/test/'
      );

      expect(tasks).toEqual(['task-001']);
    });
  });

  // ---- listOrganizations ----
  describe('listOrganizations', () => {
    test('should list orgs from prefix list', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());
      const mockListV2 = jest.fn().mockResolvedValue(
        makeMockResult(['datasets/org-a/', 'datasets/org-b/', 'datasets/org-c/'])
      );
      (registry as any).buildBucket = jest.fn().mockReturnValue({ listV2: mockListV2 });

      const orgs = await registry.listOrganizations();

      expect(orgs).toEqual(['org-a', 'org-b', 'org-c']);
    });

    test('should respect custom dataset path', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo({ ossDatasetPath: 'custom' }));
      const mockListV2 = jest.fn().mockResolvedValue(
        makeMockResult(['custom/my-org/'])
      );
      (registry as any).buildBucket = jest.fn().mockReturnValue({ listV2: mockListV2 });

      const orgs = await registry.listOrganizations();

      expect(orgs).toEqual(['my-org']);
      expect(mockListV2).toHaveBeenCalledWith(
        expect.objectContaining({ prefix: 'custom/', delimiter: '/' }),
      );
    });
  });

  // ---- listOrgDatasets ----
  describe('listOrgDatasets', () => {
    test('should list datasets for an org', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());
      const mockListV2 = jest.fn().mockResolvedValue(
        makeMockResult(['datasets/org/ds-a/', 'datasets/org/ds-b/'])
      );
      (registry as any).buildBucket = jest.fn().mockReturnValue({ listV2: mockListV2 });

      const datasets = await registry.listOrgDatasets('org');

      expect(datasets).toEqual(['ds-a', 'ds-b']);
    });
  });

  // ---- listDatasetSplits ----
  describe('listDatasetSplits', () => {
    test('should list splits for a dataset', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());
      const mockListV2 = jest.fn().mockResolvedValue(
        makeMockResult(['datasets/org/ds/train/', 'datasets/org/ds/test/'])
      );
      (registry as any).buildBucket = jest.fn().mockReturnValue({ listV2: mockListV2 });

      const splits = await registry.listDatasetSplits('org', 'ds');

      expect(splits).toEqual(['test', 'train']);
    });
  });

  // ---- listAllDatasets ----
  describe('listAllDatasets', () => {
    test('should list all (org, dataset) pairs', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());

      // Mock listOrganizations to return 2 orgs
      jest.spyOn(registry, 'listOrganizations').mockResolvedValue(['org-a', 'org-b']);
      // Mock listOrgDatasets per org
      jest.spyOn(registry, 'listOrgDatasets')
        .mockResolvedValueOnce(['ds-1', 'ds-2'])
        .mockResolvedValueOnce(['ds-3']);

      const pairs = await registry.listAllDatasets(5);

      expect(pairs).toEqual([
        ['org-a', 'ds-1'],
        ['org-a', 'ds-2'],
        ['org-b', 'ds-3'],
      ]);
    });

    test('should return empty when no orgs', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());
      jest.spyOn(registry, 'listOrganizations').mockResolvedValue([]);

      const pairs = await registry.listAllDatasets();

      expect(pairs).toEqual([]);
    });
  });

  // ---- listDatasets ----
  describe('listDatasets', () => {
    test('should list datasets with task ids for a specific org', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());

      // org_prefix -> dataset prefixes
      const mockListV2 = jest.fn()
        .mockResolvedValueOnce(makeMockResult(['datasets/org-a/ds-1/']))    // dataset list
        .mockResolvedValueOnce(makeMockResult(['datasets/org-a/ds-1/test/']))  // split list
        .mockResolvedValueOnce(makeMockResult(['datasets/org-a/ds-1/test/task-001/'])); // task list

      (registry as any).buildBucket = jest.fn().mockReturnValue({ listV2: mockListV2 });
      // Override extractTasksFromSplit to avoid extra mock complexity
      jest.spyOn(registry as any, 'extractTasksFromSplit').mockResolvedValue(['task-001']);

      const datasets = await registry.listDatasets('org-a');

      expect(datasets).toHaveLength(1);
      expect(datasets[0]).toEqual({
        id: 'org-a/ds-1',
        split: 'test',
        taskIds: ['task-001'],
      });
    });

    test('should list datasets for all orgs when org not specified', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());

      const mockListV2 = jest.fn()
        // all orgs
        .mockResolvedValueOnce(makeMockResult(['datasets/org-a/', 'datasets/org-b/']))
        // org-a datasets
        .mockResolvedValueOnce(makeMockResult(['datasets/org-a/ds-1/']))
        // org-a ds-1 splits
        .mockResolvedValueOnce(makeMockResult(['datasets/org-a/ds-1/test/']))
        // org-a ds-1 tasks
        .mockResolvedValueOnce(makeMockResult(['datasets/org-a/ds-1/test/task-001/']))
        // org-b datasets
        .mockResolvedValueOnce(makeMockResult([]));

      (registry as any).buildBucket = jest.fn().mockReturnValue({ listV2: mockListV2 });
      jest.spyOn(registry as any, 'extractTasksFromSplit').mockResolvedValue(['task-001']);

      const datasets = await registry.listDatasets();

      expect(datasets).toHaveLength(1);
      expect(datasets[0]!.id).toBe('org-a/ds-1');
    });
  });

  // ---- listDatasetTasks ----
  describe('listDatasetTasks', () => {
    test('should return DatasetSpec with tasks', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());
      jest.spyOn(registry as any, 'extractTasksFromSplit').mockResolvedValue(['task-001', 'task-002']);
      (registry as any).buildBucket = jest.fn().mockReturnValue({
        listV2: jest.fn().mockResolvedValue(makeMockResult([])),
      });

      const result = await registry.listDatasetTasks('org', 'ds', 'test');

      expect(result).toEqual({
        id: 'org/ds',
        split: 'test',
        taskIds: ['task-001', 'task-002'],
      });
    });

    test('should return null when no tasks found', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());
      jest.spyOn(registry as any, 'extractTasksFromSplit').mockResolvedValue([]);
      (registry as any).buildBucket = jest.fn().mockReturnValue({
        listV2: jest.fn().mockResolvedValue(makeMockResult([])),
      });

      const result = await registry.listDatasetTasks('org', 'ds', 'test');

      expect(result).toBeNull();
    });

    test('should default split to "test"', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());
      const extractSpy = jest.spyOn(registry as any, 'extractTasksFromSplit').mockResolvedValue(['t1']);
      (registry as any).buildBucket = jest.fn().mockReturnValue({
        listV2: jest.fn().mockResolvedValue(makeMockResult([])),
      });

      await registry.listDatasetTasks('org', 'ds');

      expect(extractSpy).toHaveBeenCalledWith(
        expect.anything(),
        'datasets/org/ds/test/'
      );
    });
  });

  // ---- uploadDataset ----
  describe('uploadDataset', () => {
    let tmpDir: string;

    beforeEach(() => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'oss-registry-test-'));
      // Create test task dirs with files
      const task1Dir = path.join(tmpDir, 'task-001');
      const task2Dir = path.join(tmpDir, 'task-002');
      fs.mkdirSync(task1Dir);
      fs.mkdirSync(task2Dir);
      fs.writeFileSync(path.join(task1Dir, 'data.json'), JSON.stringify({ key: 'value' }));
      fs.writeFileSync(path.join(task1Dir, 'README.md'), '# Task 001');
      fs.writeFileSync(path.join(task2Dir, 'data.json'), JSON.stringify({ key: 'value2' }));
    });

    afterEach(() => {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    test('should upload task directories', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());

      const mockPut = jest.fn().mockResolvedValue({ res: { status: 200 } });
      const mockListV2 = jest.fn()
        // task exists checks (return empty = not exists)
        .mockResolvedValue(makeMockObjectResult([]));
      (registry as any).buildBucket = jest.fn().mockReturnValue({
        put: mockPut,
        listV2: mockListV2,
      });

      const source: LocalDatasetConfig = { path: tmpDir, taskNames: null, excludeTaskNames: null, nTasks: null };
      const target: RegistryDatasetConfig = {
        name: 'org/test-dataset',
        registry: defaultRegistryInfo(),
        version: 'test',
        overwrite: false,
        taskNames: null,
        excludeTaskNames: null,
        nTasks: null,
        downloadDir: null,
      };

      const result = await registry.uploadDataset(source, target, 2);

      expect(result.id).toBe('org/test-dataset');
      expect(result.split).toBe('test');
      expect(result.uploaded).toBe(2);
      expect(result.skipped).toBe(0);
      expect(result.failed).toBe(0);
      expect(mockPut).toHaveBeenCalledTimes(3); // 2 json files + 1 md file
    });

    test('should skip existing tasks when overwrite is false', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());

      const mockPut = jest.fn().mockResolvedValue({ res: { status: 200 } });
      // First task exists, second doesn't
      const mockListV2 = jest.fn()
        .mockResolvedValueOnce(makeMockObjectResult(['exists']))   // task-001 check -> exists
        .mockResolvedValueOnce(makeMockObjectResult([]));           // task-002 check -> not exists

      (registry as any).buildBucket = jest.fn().mockReturnValue({
        put: mockPut,
        listV2: mockListV2,
      });

      const source: LocalDatasetConfig = { path: tmpDir, taskNames: null, excludeTaskNames: null, nTasks: null };
      const target: RegistryDatasetConfig = {
        name: 'org/test-dataset',
        registry: defaultRegistryInfo(),
        version: 'test',
        overwrite: false,
        taskNames: null,
        excludeTaskNames: null,
        nTasks: null,
        downloadDir: null,
      };

      const result = await registry.uploadDataset(source, target, 2);

      expect(result.uploaded).toBe(1);
      expect(result.skipped).toBe(1);
      expect(result.failed).toBe(0);
    });

    test('should overwrite existing tasks when overwrite is true', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());

      const mockPut = jest.fn().mockResolvedValue({ res: { status: 200 } });
      const mockListV2 = jest.fn()
        .mockResolvedValue(makeMockObjectResult(['exists'])); // both exist

      (registry as any).buildBucket = jest.fn().mockReturnValue({
        put: mockPut,
        listV2: mockListV2,
      });

      const source: LocalDatasetConfig = { path: tmpDir, taskNames: null, excludeTaskNames: null, nTasks: null };
      const target: RegistryDatasetConfig = {
        name: 'org/test-dataset',
        registry: defaultRegistryInfo(),
        version: 'test',
        overwrite: true,
        taskNames: null,
        excludeTaskNames: null,
        nTasks: null,
        downloadDir: null,
      };

      const result = await registry.uploadDataset(source, target, 2);

      expect(result.uploaded).toBe(2);
      expect(result.skipped).toBe(0);
    });

    test('should count failed uploads', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());

      const mockPut = jest.fn()
        .mockRejectedValueOnce(new Error('upload failed'))
        .mockResolvedValue({ res: { status: 200 } });
      const mockListV2 = jest.fn().mockResolvedValue(makeMockObjectResult([]));

      (registry as any).buildBucket = jest.fn().mockReturnValue({
        put: mockPut,
        listV2: mockListV2,
      });

      const source: LocalDatasetConfig = { path: tmpDir, taskNames: null, excludeTaskNames: null, nTasks: null };
      const target: RegistryDatasetConfig = {
        name: 'org/test-dataset',
        registry: defaultRegistryInfo(),
        version: 'test',
        overwrite: false,
        taskNames: null,
        excludeTaskNames: null,
        nTasks: null,
        downloadDir: null,
      };

      const result = await registry.uploadDataset(source, target, 1);

      expect(result.failed).toBe(1);
      expect(result.uploaded).toBe(1);
    });

    test('should handle version null (empty string split)', async () => {
      const registry = new OssDatasetRegistry(defaultRegistryInfo());

      const mockPut = jest.fn().mockResolvedValue({ res: { status: 200 } });
      const mockListV2 = jest.fn().mockResolvedValue(makeMockObjectResult([]));
      (registry as any).buildBucket = jest.fn().mockReturnValue({
        put: mockPut,
        listV2: mockListV2,
      });

      const source: LocalDatasetConfig = { path: tmpDir, taskNames: null, excludeTaskNames: null, nTasks: null };
      const target: RegistryDatasetConfig = {
        name: 'org/ds',
        registry: defaultRegistryInfo(),
        version: null,
        overwrite: false,
        taskNames: null,
        excludeTaskNames: null,
        nTasks: null,
        downloadDir: null,
      };

      const result = await registry.uploadDataset(source, target, 1);

      expect(result.split).toBe('');
    });
  });
});
