/**
 * DatasetClient — thin wrapper over a DatasetRegistry instance.
 *
 * Mirrors Python rock/sdk/envhub/datasets/client.py.
 * Provides a simplified interface for common dataset operations.
 */

import type { DatasetRegistry } from './registry/base.js';
import type {
  DatasetSpec,
  LocalDatasetConfig,
  RegistryDatasetConfig,
  UploadResult,
} from './models.js';

export class DatasetClient {
  private registry: DatasetRegistry;

  /**
   * @param registry - A DatasetRegistry implementation (e.g., OssDatasetRegistry)
   */
  constructor(registry: DatasetRegistry) {
    this.registry = registry;
  }

  /** List all datasets, optionally filtered by organization. */
  async listDatasets(org?: string): Promise<DatasetSpec[]> {
    return this.registry.listDatasets(org);
  }

  /** List task IDs for one dataset split. Returns null if empty. */
  async listDatasetTasks(
    organization: string,
    dataset: string,
    split: string = 'test',
  ): Promise<DatasetSpec | null> {
    return this.registry.listDatasetTasks(organization, dataset, split);
  }

  /** List organization names under the dataset registry. */
  async listOrganizations(): Promise<string[]> {
    return this.registry.listOrganizations();
  }

  /** List dataset names under one organization. */
  async listOrgDatasets(organization: string): Promise<string[]> {
    return this.registry.listOrgDatasets(organization);
  }

  /** List all (organization, dataset) pairs. */
  async listAllDatasets(concurrency: number = 10): Promise<[string, string][]> {
    return this.registry.listAllDatasets(concurrency);
  }

  /** List split names under one dataset. */
  async listDatasetSplits(organization: string, dataset: string): Promise<string[]> {
    return this.registry.listDatasetSplits(organization, dataset);
  }

  /**
   * Upload a local dataset to the registry.
   *
   * @param source - Local dataset config with filesystem path
   * @param target - Registry dataset config with destination details
   * @param concurrency - Max concurrent uploads (default 4)
   */
  async uploadDataset(
    source: LocalDatasetConfig,
    target: RegistryDatasetConfig,
    concurrency: number = 4,
  ): Promise<UploadResult> {
    return this.registry.uploadDataset(source, target, concurrency);
  }
}
