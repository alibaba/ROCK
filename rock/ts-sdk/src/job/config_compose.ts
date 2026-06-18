/**
 * ComposeJobConfig — multi-container job execution via Docker Compose inside a DinD sandbox.
 *
 * Extends JobConfig with Docker Compose topology definition: services, init containers,
 * volumes, and OSS artifact downloads.
 *
 * Usage follows the same pattern as BashJobConfig:
 *   config = ComposeJobConfig({
 *     experiment_id: "my-exp",
 *     services: [...],
 *   })
 *   result = await new Job(config).run()
 */

import { z } from 'zod';
import { _jobConfigBaseFields, _syncExperimentId } from './config';

// Re-export for internal use by ComposeJobConfigSchema
export { _jobConfigBaseFields, _syncExperimentId };

// ---------------------------------------------------------------------------
// ResourceConfig
// ---------------------------------------------------------------------------

export const ResourceConfigSchema = z.object({
  cpu: z.union([z.string(), z.number()]).default('1'),
  memory: z.string().default('2Gi'),
});

export type ResourceConfig = z.infer<typeof ResourceConfigSchema>;

// ---------------------------------------------------------------------------
// VolumeMount
// ---------------------------------------------------------------------------

export const VolumeMountSchema = z.object({
  name: z.string().min(1, 'name is required'),
  mount_path: z.string().min(1, 'mount_path is required'),
  read_only: z.boolean().default(false),
});

export type VolumeMount = z.infer<typeof VolumeMountSchema>;

// ---------------------------------------------------------------------------
// VolumeConfig
// ---------------------------------------------------------------------------

export const VolumeConfigSchema = z.object({
  name: z.string().min(1, 'name is required'),
  host_path: z.string().nullable().default(null),
});

export type VolumeConfig = z.infer<typeof VolumeConfigSchema>;

// ---------------------------------------------------------------------------
// ServiceConfig
// ---------------------------------------------------------------------------

export const ServiceConfigSchema = z.object({
  name: z.string().min(1, 'name is required'),
  image: z.string().min(1, 'image is required'),
  command: z.array(z.string()).nullable().default(null),
  args: z.array(z.string()).nullable().default(null),
  script: z.string().nullable().default(null),
  env: z.record(z.string()).default({}),
  ports: z.array(z.number().int()).default([]),
  resources: ResourceConfigSchema.nullable().default(null),
  privileged: z.boolean().default(false),
  volume_mounts: z.array(VolumeMountSchema).default([]),
  is_main: z.boolean().default(false),
});

export type ServiceConfig = z.infer<typeof ServiceConfigSchema>;

// ---------------------------------------------------------------------------
// InitContainerConfig
// ---------------------------------------------------------------------------

export const InitContainerConfigSchema = z.object({
  name: z.string().min(1, 'name is required'),
  image: z.string().min(1, 'image is required'),
  command: z.array(z.string()).nullable().default(null),
  args: z.array(z.string()).nullable().default(null),
  script: z.string().nullable().default(null),
  volume_mounts: z.array(VolumeMountSchema).default([]),
});

export type InitContainerConfig = z.infer<typeof InitContainerConfigSchema>;

// ---------------------------------------------------------------------------
// OSSArtifactConfig
// ---------------------------------------------------------------------------

export const OSSArtifactConfigSchema = z.object({
  name: z.string().min(1, 'name is required'),
  oss_key: z.string().min(1, 'oss_key is required'),
  target_path: z.string().default('/tmp/shared'),
  archive: z.boolean().default(true),
});

export type OSSArtifactConfig = z.infer<typeof OSSArtifactConfigSchema>;

// ---------------------------------------------------------------------------
// ComposeJobConfig
// ---------------------------------------------------------------------------

/**
 * Generate timestamp-based default job_name (Python: datetime.now().strftime(...)).
 */
function _generateTimestampName(): string {
  const now = new Date();
  const Y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, '0');
  const d = String(now.getDate()).padStart(2, '0');
  const H = String(now.getHours()).padStart(2, '0');
  const M = String(now.getMinutes()).padStart(2, '0');
  const S = String(now.getSeconds()).padStart(2, '0');
  return `${Y}-${m}-${d}__${H}-${M}-${S}`;
}

/**
 * Create a ComposeJobConfig schema parameterized by the environment type.
 *
 * Defines a multi-container topology executed inside a single DinD sandbox.
 * Exactly one service must have is_main=true — its exit code determines
 * the job's success/failure.
 */
export function ComposeJobConfigSchema<TEnv extends z.ZodTypeAny>(environmentSchema: TEnv) {
  return z
    .object({
      environment: environmentSchema.default({}),
      ..._jobConfigBaseFields(),
      // ComposeJobConfig overrides job_name default to timestamp (matching Python)
      job_name: z.string().default(_generateTimestampName),

      services: z.array(ServiceConfigSchema).min(1, 'At least one service is required'),
      init_containers: z.array(InitContainerConfigSchema).default([]),
      volumes: z.array(VolumeConfigSchema).default([]),
      oss_artifacts: z.array(OSSArtifactConfigSchema).default([]),
      network_mode: z.enum(['host', 'bridge']).default('host'),
      callback_url: z.string().nullable().default(null),
    })
    .strict()
    .superRefine((data, ctx) => {
      // _validate_main_service: exactly one service must have is_main=true
      const mains = data.services.filter((s) => s.is_main);
      if (mains.length !== 1) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: `Exactly one service must have is_main=true, found ${mains.length}: ${mains.map(s => s.name).join(', ')}`,
          path: ['services'],
        });
      }
    })
    .transform((data) => {
      _syncExperimentId(data as unknown as Record<string, unknown>);
      return data;
    });
}

export type ComposeJobConfig = z.infer<ReturnType<typeof ComposeJobConfigSchema<z.ZodTypeAny>>>;
