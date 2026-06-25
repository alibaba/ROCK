/**
 * OSS Dataset Registry implementation
 *
 * Mirrors Python rock/sdk/envhub/datasets/registry/oss.py.
 * Uses ali-oss Node.js SDK for OSS operations (lazy-loaded to avoid
 * ESM import issues in Jest tests).
 */

import * as fs from 'fs';
import * as path from 'path';

import { initLogger } from '../../../logger.js';

import type { DatasetRegistry } from './base.js';
import type {
  DatasetSpec,
  LocalDatasetConfig,
  OssRegistryInfo,
  RegistryDatasetConfig,
  UploadResult,
} from '../models.js';

const logger = initLogger('rock.sdk.envhub.datasets.registry.oss');

// ---------------------------------------------------------------------------
// Concurrency helpers
// ---------------------------------------------------------------------------

/** Simple async semaphore for bounded concurrency. */
async function withConcurrency<T>(
  items: T[],
  concurrency: number,
  fn: (item: T) => Promise<void>,
): Promise<void> {
  const executing: Promise<void>[] = [];
  for (const item of items) {
    const p = fn(item).then(() => {
      void executing.splice(executing.indexOf(p), 1);
    });
    executing.push(p);
    if (executing.length >= concurrency) {
      await Promise.race(executing);
    }
  }
  await Promise.all(executing);
}

/** Run async tasks with bounded concurrency, collecting results in a Map. */
async function poolMap<K extends string, V>(
  entries: [K, () => Promise<V>][],
  concurrency: number,
): Promise<Map<K, V | Error>> {
  const results = new Map<K, V | Error>();
  const tasks = entries.map(([key, fn]) => ({ key, fn }));
  await withConcurrency(tasks, concurrency, async ({ key, fn }) => {
    try {
      results.set(key, await fn());
    } catch (e) {
      results.set(key, e instanceof Error ? e : new Error(String(e)));
    }
  });
  return results;
}

// ---------------------------------------------------------------------------
// OssDatasetRegistry
// ---------------------------------------------------------------------------

export class OssDatasetRegistry implements DatasetRegistry {
  private registry: OssRegistryInfo;

  constructor(registry: OssRegistryInfo) {
    this.registry = registry;
  }

  // ---- bucket ----

  /**
   * Lazily creates an OSS bucket client.
   * Uses dynamic import to avoid ESM issues in test environments.
   * The bucket is typed as any to match the existing OssClient pattern
   * in sandbox/oss_client.ts.
   */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private async buildBucket(): Promise<any> {
    // Dynamic import to avoid ESM resolution issues in Jest
    const OSS = await import('ali-oss');
    return new OSS.default({
      accessKeyId: this.registry.ossAccessKeyId ?? '',
      accessKeySecret: this.registry.ossAccessKeySecret ?? '',
      endpoint: this.registry.ossEndpoint ?? undefined,
      bucket: this.registry.ossBucket ?? undefined,
      region: this.registry.ossRegion ?? undefined,
    });
  }

  // ---- prefix ----

  buildPrefix(org: string, name: string, split?: string): string {
    const base = this.registry.ossDatasetPath ?? 'datasets';
    const parts = [base, org, name];
    if (split) {
      parts.push(split);
    }
    return parts.join('/');
  }

  static lastSegment(prefix: string): string {
    const trimmed = prefix.endsWith('/') ? prefix.slice(0, -1) : prefix;
    const idx = trimmed.lastIndexOf('/');
    return idx === -1 ? trimmed : trimmed.slice(idx + 1);
  }

  // ---- task extraction ----

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private async extractTasksFromSplit(bucket: any, splitPrefix: string): Promise<string[]> {
    const result = await bucket.listV2({
      prefix: splitPrefix, delimiter: '/', 'max-keys': 1000,
    });

    // Directory tasks from prefixes
    const dirTasks: string[] = (result.prefixes ?? []).map((p: string) =>
      OssDatasetRegistry.lastSegment(p),
    );

    // File tasks from objects: direct files under split, strip suffix
    const fileTasks: string[] = [];
    for (const obj of result.objects ?? []) {
      const key: string = obj.name;
      // Ignore directory placeholder objects (key ending with "/")
      if (key.endsWith('/')) continue;
      // Get the relative path from splitPrefix
      const relative = key.slice(splitPrefix.length);
      // Only direct files (no nested paths with "/")
      if (relative.includes('/')) continue;
      // Strip suffix (e.g., "task-001.json" -> "task-001")
      const dotIdx = relative.lastIndexOf('.');
      const name = dotIdx === -1 ? relative : relative.slice(0, dotIdx);
      fileTasks.push(name);
    }

    // Merge and dedupe with stable sort
    const allTasks = [...new Set([...dirTasks, ...fileTasks])].sort();
    return allTasks;
  }

  // ---- list operations ----

  async listOrganizations(): Promise<string[]> {
    const bucket = await this.buildBucket();
    const base = this.registry.ossDatasetPath ?? 'datasets';
    const result = await bucket.listV2({
      prefix: `${base}/`, delimiter: '/', 'max-keys': 1000,
    });
    return (result.prefixes ?? [])
      .map((p: string) => OssDatasetRegistry.lastSegment(p))
      .sort();
  }

  async listOrgDatasets(organization: string): Promise<string[]> {
    const bucket = await this.buildBucket();
    const base = this.registry.ossDatasetPath ?? 'datasets';
    const result = await bucket.listV2({
      prefix: `${base}/${organization}/`, delimiter: '/', 'max-keys': 1000,
    });
    return (result.prefixes ?? [])
      .map((p: string) => OssDatasetRegistry.lastSegment(p))
      .sort();
  }

  async listDatasetSplits(organization: string, dataset: string): Promise<string[]> {
    const bucket = await this.buildBucket();
    const base = this.registry.ossDatasetPath ?? 'datasets';
    const result = await bucket.listV2({
      prefix: `${base}/${organization}/${dataset}/`, delimiter: '/', 'max-keys': 1000,
    });
    return (result.prefixes ?? [])
      .map((p: string) => OssDatasetRegistry.lastSegment(p))
      .sort();
  }

  async listAllDatasets(concurrency: number = 10): Promise<[string, string][]> {
    const orgs = await this.listOrganizations();
    if (orgs.length === 0) return [];

    // Parallel org queries with bounded concurrency
    const orgDatasets = await Promise.all(
      orgs.map(async (org) => {
        const datasets = await this.listOrgDatasets(org);
        return { org, datasets };
      }),
    );

    const pairs: [string, string][] = [];
    for (const { org, datasets } of orgDatasets) {
      for (const ds of datasets) {
        pairs.push([org, ds]);
      }
    }
    return pairs.sort();
  }

  async listDatasets(organization?: string): Promise<DatasetSpec[]> {
    const bucket = await this.buildBucket();
    const base = this.registry.ossDatasetPath ?? 'datasets';

    let orgPrefixes: string[];
    if (organization) {
      orgPrefixes = [`${base}/${organization}/`];
    } else {
      const result = await bucket.listV2({
        prefix: `${base}/`, delimiter: '/', 'max-keys': 1000,
      });
      orgPrefixes = result.prefixes ?? [];
    }

    const datasets: DatasetSpec[] = [];
    for (const orgPrefix of orgPrefixes) {
      const org = OssDatasetRegistry.lastSegment(orgPrefix);

      const dsResult = await bucket.listV2({
        prefix: orgPrefix, delimiter: '/', 'max-keys': 1000,
      });
      for (const namePrefix of dsResult.prefixes ?? []) {
        const name = OssDatasetRegistry.lastSegment(namePrefix);

        const splitResult = await bucket.listV2({
          prefix: namePrefix, delimiter: '/', 'max-keys': 1000,
        });
        for (const splitPrefix of splitResult.prefixes ?? []) {
          const split = OssDatasetRegistry.lastSegment(splitPrefix);
          const taskIds = await this.extractTasksFromSplit(bucket, splitPrefix);
          datasets.push({
            id: `${org}/${name}`,
            split,
            taskIds,
          });
        }
      }
    }

    return datasets;
  }

  async listDatasetTasks(
    organization: string,
    dataset: string,
    split: string = 'test',
  ): Promise<DatasetSpec | null> {
    const bucket = await this.buildBucket();
    const splitPrefix = `${this.buildPrefix(organization, dataset, split)}/`;
    const taskIds = await this.extractTasksFromSplit(bucket, splitPrefix);

    if (taskIds.length === 0) return null;

    return {
      id: `${organization}/${dataset}`,
      split,
      taskIds,
    };
  }

  // ---- upload operations ----

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private async taskExists(bucket: any, taskPrefix: string): Promise<boolean> {
    const result = await bucket.listV2({
      prefix: taskPrefix, 'max-keys': 1,
    });
    return (result.objects ?? []).length > 0;
  }

  /** Collect all files recursively under a directory. */
  private collectFiles(dir: string): string[] {
    const files: string[] = [];
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        files.push(...this.collectFiles(fullPath));
      } else if (entry.isFile()) {
        files.push(fullPath);
      }
    }
    return files;
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private async uploadTask(
    bucket: any,
    org: string,
    name: string,
    split: string,
    taskDir: string,
    overwrite: boolean,
  ): Promise<number | null> {
    const taskId = path.basename(taskDir);
    const base = this.registry.ossDatasetPath ?? 'datasets';
    const taskPrefix = `${base}/${org}/${name}/${split}/${taskId}/`;

    if (!overwrite && (await this.taskExists(bucket, taskPrefix))) {
      return null; // skipped
    }

    const files = this.collectFiles(taskDir);
    for (const file of files) {
      const key = `${taskPrefix}${path.relative(taskDir, file)}`;
      const content = fs.readFileSync(file);
      await bucket.put(key, content);
    }
    return files.length;
  }

  async uploadDataset(
    source: LocalDatasetConfig,
    target: RegistryDatasetConfig,
    concurrency: number = 4,
  ): Promise<UploadResult> {
    const [org, name] = target.name.split('/', 2);
    const split = target.version ?? '';
    const overwrite = target.overwrite;
    const localDir = source.path;

    const bucket = await this.buildBucket();
    const entries = fs.readdirSync(localDir, { withFileTypes: true });
    const taskDirs = entries
      .filter((e) => e.isDirectory())
      .map((e) => path.join(localDir, e.name))
      .sort();

    const raw = await poolMap<string, number | null>(
      taskDirs.map((d) => [
        path.basename(d),
        () => this.uploadTask(bucket, org!, name!, split, d, overwrite),
      ]),
      concurrency,
    );

    let uploaded = 0;
    let skipped = 0;
    let failed = 0;
    const sortedKeys = [...raw.keys()].sort();
    for (const taskId of sortedKeys) {
      const outcome = raw.get(taskId);
      if (outcome instanceof Error) {
        failed++;
        logger.error('Failed to upload task %s: %s', taskId, outcome.message);
      } else if (outcome === null) {
        skipped++;
        logger.info('Skipped task %s (already exists)', taskId);
      } else {
        uploaded++;
        logger.info('Uploaded task %s (%d files)', taskId, outcome);
      }
    }

    return {
      id: `${org}/${name}`,
      split,
      uploaded,
      skipped,
      failed,
    };
  }
}
