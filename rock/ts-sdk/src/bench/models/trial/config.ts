/**
 * Trial configuration models — aligned with rock.sdk.bench.models.trial.config
 *
 * RockEnvironmentConfig combines SandboxConfig (envhub-level) with Harbor's
 * EnvironmentConfig via "multiple inheritance" — all fields are flattened into
 * one Zod schema.
 */

import { z } from 'zod';
import { EnvironmentTypeSchema } from '../environment_type.js';

// ---------------------------------------------------------------------------
// AgentConfig
// ---------------------------------------------------------------------------

export const AgentConfigSchema = z.object({
  name: z.string().nullable().default(null),
  import_path: z.string().nullable().default(null),
  model_name: z.string().nullable().default(null),
  override_timeout_sec: z.number().nullable().default(null),
  override_setup_timeout_sec: z.number().nullable().default(null),
  max_timeout_sec: z.number().nullable().default(null),
  kwargs: z.record(z.unknown()).default({}),
  env: z.record(z.string()).default({}),
});

export type AgentConfig = z.infer<typeof AgentConfigSchema>;

export function createAgentConfig(config?: Partial<AgentConfig>): AgentConfig {
  return AgentConfigSchema.parse(config ?? {});
}

// ---------------------------------------------------------------------------
// EnvironmentConfig — Harbor-level environment fields
// ---------------------------------------------------------------------------

export const EnvironmentConfigSchema = z.object({
  type: EnvironmentTypeSchema.nullable().default(null),
  import_path: z.string().nullable().default(null),
  force_build: z.boolean().default(false),
  delete: z.boolean().default(true),
  override_cpus: z.number().int().nullable().default(null),
  override_memory_mb: z.number().int().nullable().default(null),
  override_storage_mb: z.number().int().nullable().default(null),
  override_gpus: z.number().int().nullable().default(null),
  suppress_override_warnings: z.boolean().default(false),
  mounts_json: z.array(z.record(z.unknown())).nullable().default(null),
  oss_mirror: z.any().nullable().default(null), // OssMirrorConfig — imported lazily by caller
  tracking: z.any().nullable().default(null),   // TrackingConfig — imported lazily by caller
  oss_deps: z.record(z.string()).default({}),
  env: z.record(z.string()).default({}),
  kwargs: z.record(z.unknown()).default({}),
});

export type EnvironmentConfig = z.infer<typeof EnvironmentConfigSchema>;

export function createEnvironmentConfig(config?: Partial<EnvironmentConfig>): EnvironmentConfig {
  return EnvironmentConfigSchema.parse(config ?? {});
}

// ---------------------------------------------------------------------------
// RockEnvironmentConfig — combined SandboxConfig + envhub + Harbor
//
// TODO: When SandboxConfig, OssMirrorConfig, TrackingConfig, ProxyConfig
// schemas are in envhub/schema.ts, import them here and use them in the
// oss_mirror / proxy / tracking fields. For now, z.any() accepts whatever
// the caller passes.
// ---------------------------------------------------------------------------

/** Partial SandboxConfig fields used as the sandbox foundation. */
const SandboxConfigPartialSchema = z.object({
  image: z.string().default('python:3.11'),
  image_os: z.string().default('linux'),
  auto_clear_seconds: z.number().default(300),
  route_key: z.string().nullable().default(null),
  startup_timeout: z.number().default(180),
  memory: z.string().default('8g'),
  cpus: z.number().default(2),
  limit_cpus: z.number().nullable().default(null),
  num_gpus: z.number().nullable().default(null),
  accelerator_type: z.string().nullable().default(null),
  user_id: z.string().nullable().default(null),
  experiment_id: z.string().nullable().default(null),
  cluster: z.string().default('zb'),
  namespace: z.string().nullable().default(null),
  registry_username: z.string().nullable().default(null),
  registry_password: z.string().nullable().default(null),
  use_kata_runtime: z.boolean().default(false),
  sandbox_id: z.string().nullable().default(null),
  auto_delete_seconds: z.number().int().nullable().default(null),
});

/** envhub EnvironmentConfig fields (uploads, oss_mirror, proxy, tracking). */
const EnvHubFieldsSchema = z.object({
  uploads: z.array(z.tuple([z.string(), z.string()])).default([]),
  env: z.record(z.string()).default({}),
  oss_mirror: z.any().nullable().default(null),
  proxy: z.any().nullable().default(null),
  tracking: z.any().nullable().default(null),
});

/**
 * RockEnvironmentConfig — unified schema combining Sandbox + envhub + Harbor env fields.
 *
 * Python: RockEnvironmentConfig(EnvironmentConfig, EnvironmentConfig)
 * — inherits from envhub EnvironmentConfig (which extends SandboxConfig)
 *   AND harbor's EnvironmentConfig.
 *
 * The merged result has:
 *  - SandboxConfig fields (image, memory, cpus, namespace, etc.)
 *  - envhub-level fields (uploads, env, oss_mirror, proxy, tracking)
 *  - Harbor-level fields (force_build, override_cpus, delete, oss_deps, etc.)
 */
export const RockEnvironmentConfigSchema = SandboxConfigPartialSchema
  .merge(EnvHubFieldsSchema)
  .merge(
    z.object({
      type: EnvironmentTypeSchema.nullable().default(null),
      import_path: z.string().nullable().default(null),
      force_build: z.boolean().default(false),
      delete: z.boolean().default(true),
      override_cpus: z.number().int().nullable().default(null),
      override_memory_mb: z.number().int().nullable().default(null),
      override_storage_mb: z.number().int().nullable().default(null),
      override_gpus: z.number().int().nullable().default(null),
      suppress_override_warnings: z.boolean().default(false),
      mounts_json: z.array(z.record(z.unknown())).nullable().default(null),
      oss_deps: z.record(z.string()).default({}),
      kwargs: z.record(z.unknown()).default({}),
    })
  );

export type RockEnvironmentConfig = z.infer<typeof RockEnvironmentConfigSchema>;

/**
 * Strip Rock-only sandbox fields and return only harbor-native environment fields.
 *
 * Mirrors Python RockEnvironmentConfig.to_harbor_environment(), which uses
 * model_validate() on the base EnvironmentConfig to auto-discard unknown fields.
 */
export function toHarborEnvironment(config: RockEnvironmentConfig): EnvironmentConfig {
  return createEnvironmentConfig({
    type: config.type,
    import_path: config.import_path,
    force_build: config.force_build,
    delete: config.delete,
    override_cpus: config.override_cpus,
    override_memory_mb: config.override_memory_mb,
    override_storage_mb: config.override_storage_mb,
    override_gpus: config.override_gpus,
    suppress_override_warnings: config.suppress_override_warnings,
    mounts_json: config.mounts_json,
    oss_mirror: config.oss_mirror,
    tracking: config.tracking,
    oss_deps: config.oss_deps,
    env: config.env,
    kwargs: config.kwargs,
  });
}

// ---------------------------------------------------------------------------
// VerifierConfig (and sub-models: TemplateConfig, NativeConfig)
// ---------------------------------------------------------------------------

export const TemplateConfigSchema = z.object({
  name: z.string().nullable().default(null),
  revision: z.string().nullable().default(null),
});

export type TemplateConfig = z.infer<typeof TemplateConfigSchema>;

export const NativeConfigSchema = z.object({
  image: z.string().nullable().default(null),
  script: z.string().nullable().default(null),
  oss_deps: z.record(z.string()).default({}),
  template: TemplateConfigSchema.nullable().default(null),
});

export type NativeConfig = z.infer<typeof NativeConfigSchema>;

export const VerifierConfigSchema = z.object({
  override_timeout_sec: z.number().nullable().default(null),
  max_timeout_sec: z.number().nullable().default(null),
  disable: z.boolean().default(false),
  patch: z.boolean().nullable().default(null),
  mode: z.enum(['harbor', 'native']).nullable().default(null),
  native_config: NativeConfigSchema.default({}),
});

export type VerifierConfig = z.infer<typeof VerifierConfigSchema>;

export function createVerifierConfig(config?: Partial<VerifierConfig>): VerifierConfig {
  return VerifierConfigSchema.parse(config ?? {});
}

// ---------------------------------------------------------------------------
// TaskConfig
// ---------------------------------------------------------------------------

export const TaskConfigSchema = z.object({
  path: z.string().min(1, 'path is required'),
  git_url: z.string().nullable().default(null),
  git_commit_id: z.string().nullable().default(null),
  overwrite: z.boolean().default(false),
  download_dir: z.string().nullable().default(null),
  source: z.string().nullable().default(null),
});

export type TaskConfig = z.infer<typeof TaskConfigSchema>;

export function createTaskConfig(config: Pick<TaskConfig, 'path'> & Partial<TaskConfig>): TaskConfig {
  return TaskConfigSchema.parse(config);
}

// ---------------------------------------------------------------------------
// ArtifactConfig
// ---------------------------------------------------------------------------

export const ArtifactConfigSchema = z.object({
  source: z.string().min(1, 'source is required'),
  destination: z.string().nullable().default(null),
});

export type ArtifactConfig = z.infer<typeof ArtifactConfigSchema>;

export function createArtifactConfig(config: Pick<ArtifactConfig, 'source'> & Partial<ArtifactConfig>): ArtifactConfig {
  return ArtifactConfigSchema.parse(config);
}
