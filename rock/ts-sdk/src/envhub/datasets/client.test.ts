/**
 * Tests for DatasetClient
 */

import { DatasetClient } from './client.js';
import type { DatasetRegistry } from './registry/base.js';
import type { DatasetSpec, LocalDatasetConfig, RegistryDatasetConfig, UploadResult } from './models.js';

// ---------------------------------------------------------------------------
// Mock registry
// ---------------------------------------------------------------------------

function mockRegistry(): DatasetRegistry {
  return {
    listDatasets: jest.fn(),
    listDatasetTasks: jest.fn(),
    listOrganizations: jest.fn(),
    listOrgDatasets: jest.fn(),
    listDatasetSplits: jest.fn(),
    listAllDatasets: jest.fn(),
    uploadDataset: jest.fn(),
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('DatasetClient', () => {
  describe('listDatasets', () => {
    test('should delegate to registry.listDatasets', async () => {
      const registry = mockRegistry();
      const expected: DatasetSpec[] = [
        { id: 'org/ds', split: 'test', taskIds: ['t1'] },
      ];
      (registry.listDatasets as jest.Mock).mockResolvedValue(expected);

      const client = new DatasetClient(registry);
      const result = await client.listDatasets('org');

      expect(result).toEqual(expected);
      expect(registry.listDatasets).toHaveBeenCalledWith('org');
    });

    test('should call without org when not provided', async () => {
      const registry = mockRegistry();
      (registry.listDatasets as jest.Mock).mockResolvedValue([]);

      const client = new DatasetClient(registry);
      await client.listDatasets();

      expect(registry.listDatasets).toHaveBeenCalledWith(undefined);
    });
  });

  describe('listDatasetTasks', () => {
    test('should delegate to registry.listDatasetTasks', async () => {
      const registry = mockRegistry();
      const expected: DatasetSpec = {
        id: 'org/ds',
        split: 'test',
        taskIds: ['task-001', 'task-002'],
      };
      (registry.listDatasetTasks as jest.Mock).mockResolvedValue(expected);

      const client = new DatasetClient(registry);
      const result = await client.listDatasetTasks('org', 'ds', 'test');

      expect(result).toEqual(expected);
    });

    test('should pass through null result', async () => {
      const registry = mockRegistry();
      (registry.listDatasetTasks as jest.Mock).mockResolvedValue(null);

      const client = new DatasetClient(registry);
      const result = await client.listDatasetTasks('org', 'ds');

      expect(result).toBeNull();
    });
  });

  describe('listOrganizations', () => {
    test('should delegate to registry.listOrganizations', async () => {
      const registry = mockRegistry();
      (registry.listOrganizations as jest.Mock).mockResolvedValue(['org-a', 'org-b']);

      const client = new DatasetClient(registry);
      const result = await client.listOrganizations();

      expect(result).toEqual(['org-a', 'org-b']);
    });
  });

  describe('listOrgDatasets', () => {
    test('should delegate to registry.listOrgDatasets', async () => {
      const registry = mockRegistry();
      (registry.listOrgDatasets as jest.Mock).mockResolvedValue(['ds-1', 'ds-2']);

      const client = new DatasetClient(registry);
      const result = await client.listOrgDatasets('my-org');

      expect(result).toEqual(['ds-1', 'ds-2']);
    });
  });

  describe('listAllDatasets', () => {
    test('should delegate to registry.listAllDatasets', async () => {
      const registry = mockRegistry();
      (registry.listAllDatasets as jest.Mock).mockResolvedValue([
        ['org-a', 'ds-1'],
        ['org-b', 'ds-2'],
      ]);

      const client = new DatasetClient(registry);
      const result = await client.listAllDatasets(5);

      expect(result).toEqual([['org-a', 'ds-1'], ['org-b', 'ds-2']]);
    });
  });

  describe('listDatasetSplits', () => {
    test('should delegate to registry.listDatasetSplits', async () => {
      const registry = mockRegistry();
      (registry.listDatasetSplits as jest.Mock).mockResolvedValue(['train', 'test']);

      const client = new DatasetClient(registry);
      const result = await client.listDatasetSplits('org', 'ds');

      expect(result).toEqual(['train', 'test']);
    });
  });

  describe('uploadDataset', () => {
    test('should delegate to registry.uploadDataset', async () => {
      const registry = mockRegistry();
      const expected: UploadResult = {
        id: 'org/ds',
        split: 'test',
        uploaded: 10,
        skipped: 2,
        failed: 0,
      };
      (registry.uploadDataset as jest.Mock).mockResolvedValue(expected);

      const source: LocalDatasetConfig = { path: '/tmp/ds', taskNames: null, excludeTaskNames: null, nTasks: null };
      const target: RegistryDatasetConfig = {
        name: 'org/ds',
        registry: { split: null, revision: null, ossDatasetPath: null, ossAccessKeyId: null, ossAccessKeySecret: null, ossRegion: null, ossEndpoint: null, ossBucket: null },
        version: 'test',
        overwrite: false,
        taskNames: null,
        excludeTaskNames: null,
        nTasks: null,
        downloadDir: null,
      };

      const client = new DatasetClient(registry);
      const result = await client.uploadDataset(source, target, 4);

      expect(result).toEqual(expected);
      expect(registry.uploadDataset).toHaveBeenCalledWith(source, target, 4);
    });
  });
});
