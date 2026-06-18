/**
 * Datasets models — Zod schemas and TypeScript types
 *
 * Mirrors Python rock/sdk/envhub/datasets/models.py and
 * rock/sdk/envhub/config.py (OssMirrorConfig), plus the
 * registry/dataset config types from rock/sdk/bench/models/job/config.py
 * that DatasetClient and DatasetRegistry consume directly.
 */

import { z } from 'zod';

// ---------------------------------------------------------------------------
// DatasetSpec
// ---------------------------------------------------------------------------

export const DatasetSpecSchema = z.object({
  /** "{organization}/{dataset_name}", e.g. "princeton-nlp/SWE-bench_Verified" */
  id: z.string(),
  split: z.string(),
  taskIds: z.array(z.string()).default([]),
});

export type DatasetSpec = z.infer<typeof DatasetSpecSchema>;

// ---------------------------------------------------------------------------
// UploadResult
// ---------------------------------------------------------------------------

export const UploadResultSchema = z.object({
  /** "{organization}/{dataset_name}" */
  id: z.string(),
  split: z.string(),
  uploaded: z.number().int().nonnegative(),
  skipped: z.number().int().nonnegative(),
  failed: z.number().int().nonnegative(),
});

export type UploadResult = z.infer<typeof UploadResultSchema>;

// ---------------------------------------------------------------------------
// OssRegistryInfo — OSS registry connection details
// ---------------------------------------------------------------------------

export const OssRegistryInfoSchema = z.object({
  split: z.string().nullable().optional().default(null),
  revision: z.string().nullable().optional().default(null),
  ossDatasetPath: z.string().nullable().optional().default(null),
  ossAccessKeyId: z.string().nullable().optional().default(null),
  ossAccessKeySecret: z.string().nullable().optional().default(null),
  ossRegion: z.string().nullable().optional().default(null),
  ossEndpoint: z.string().nullable().optional().default(null),
  ossBucket: z.string().nullable().optional().default(null),
});

export type OssRegistryInfo = z.infer<typeof OssRegistryInfoSchema>;

// ---------------------------------------------------------------------------
// LocalDatasetConfig — local filesystem dataset
// ---------------------------------------------------------------------------

export const LocalDatasetConfigSchema = z.object({
  /** Local filesystem path to the dataset directory */
  path: z.string(),
  taskNames: z.array(z.string()).nullable().optional().default(null),
  excludeTaskNames: z.array(z.string()).nullable().optional().default(null),
  nTasks: z.number().int().positive().nullable().optional().default(null),
});

export type LocalDatasetConfig = z.infer<typeof LocalDatasetConfigSchema>;

// ---------------------------------------------------------------------------
// RegistryDatasetConfig — remote registry dataset
// ---------------------------------------------------------------------------

export const RegistryDatasetConfigSchema = z.object({
  /** Dataset name, format: "{organization}/{dataset_name}" */
  name: z.string(),
  /** Registry connection info (OSS for now; union later) */
  registry: OssRegistryInfoSchema,
  version: z.string().nullable().optional().default(null),
  overwrite: z.boolean().default(false),
  taskNames: z.array(z.string()).nullable().optional().default(null),
  excludeTaskNames: z.array(z.string()).nullable().optional().default(null),
  nTasks: z.number().int().positive().nullable().optional().default(null),
  downloadDir: z.string().nullable().optional().default(null),
});

export type RegistryDatasetConfig = z.infer<typeof RegistryDatasetConfigSchema>;

// ---------------------------------------------------------------------------
// OssMirrorConfig — OSS artifact mirror configuration
// ---------------------------------------------------------------------------

export const OssMirrorConfigSchema = z.object({
  enabled: z.boolean().default(false),
  ossBucket: z.string().nullable().optional().default(null),
  namespace: z.string().nullable().optional().default(null),
  experimentId: z.string().nullable().optional().default(null),
  ossAccessKeyId: z.string().nullable().optional().default(null),
  ossAccessKeySecret: z.string().nullable().optional().default(null),
  ossRegion: z.string().nullable().optional().default(null),
  ossEndpoint: z.string().nullable().optional().default(null),
});

export type OssMirrorConfig = z.infer<typeof OssMirrorConfigSchema>;
