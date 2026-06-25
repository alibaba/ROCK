/**
 * Datasets module barrel exports
 *
 * Mirrors Python rock/sdk/envhub/datasets/__init__.py.
 */

export { DatasetClient } from './client.js';
export {
  DatasetSpecSchema,
  UploadResultSchema,
  OssRegistryInfoSchema,
  LocalDatasetConfigSchema,
  RegistryDatasetConfigSchema,
  OssMirrorConfigSchema,
} from './models.js';
export type {
  DatasetSpec,
  UploadResult,
  OssRegistryInfo,
  LocalDatasetConfig,
  RegistryDatasetConfig,
  OssMirrorConfig,
} from './models.js';

export type { DatasetRegistry } from './registry/base.js';
export { OssDatasetRegistry } from './registry/oss.js';
