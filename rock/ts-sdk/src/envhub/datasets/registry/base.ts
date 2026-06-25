/**
 * Abstract DatasetRegistry interface
 *
 * Mirrors Python rock/sdk/envhub/datasets/registry/base.py (BaseDatasetRegistry).
 * TypeScript interface (structural typing) instead of Python ABC (nominal typing).
 */

import type { DatasetSpec, LocalDatasetConfig, RegistryDatasetConfig, UploadResult } from '../models.js';

/**
 * Contract for dataset storage backends (OSS, HF, local, remote, etc.).
 *
 * Implementations handle storage-specific details — OSS bucket operations,
 * HuggingFace Hub API, local filesystem traversal, etc.
 */
export interface DatasetRegistry {
  /**
   * List all datasets, optionally filtered by organization.
   * Returns full DatasetSpec (with task IDs) for each dataset + split combination.
   */
  listDatasets(organization?: string): Promise<DatasetSpec[]>;

  /**
   * List task IDs for one dataset split.
   * Returns null if the dataset/split has no tasks.
   */
  listDatasetTasks(organization: string, dataset: string, split?: string): Promise<DatasetSpec | null>;

  /**
   * List organization names under the dataset registry.
   * Single backend call.
   */
  listOrganizations(): Promise<string[]>;

  /**
   * List dataset names under one organization.
   * Single backend call.
   */
  listOrgDatasets(organization: string): Promise<string[]>;

  /**
   * List split names under one dataset.
   * Single backend call.
   */
  listDatasetSplits(organization: string, dataset: string): Promise<string[]>;

  /**
   * List all (organization, dataset) pairs.
   * Uses 1 + N_org backend calls with bounded concurrency.
   */
  listAllDatasets(concurrency?: number): Promise<[string, string][]>;

  /**
   * Upload source.path/{task_id}/ subdirs to target registry location.
   *
   * @param source - Local dataset config with filesystem path
   * @param target - Registry dataset config with destination name/version
   * @param concurrency - Max concurrent upload workers (default 4)
   */
  uploadDataset(source: LocalDatasetConfig, target: RegistryDatasetConfig, concurrency?: number): Promise<UploadResult>;
}
