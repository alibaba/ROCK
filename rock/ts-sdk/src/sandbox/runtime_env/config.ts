import { z } from 'zod';

/**
 * Base configuration for runtime environments.
 */
export const RuntimeEnvConfigSchema = z.object({
  /** Runtime type discriminator. */
  type: z.string(),

  /** Runtime version. Use 'default' for the default version of each runtime. */
  version: z.string().default('default'),

  /** Environment variables for the runtime session. */
  env: z.record(z.string()).default({}),

  /** Timeout in seconds for installation commands. */
  installTimeout: z.number().int().positive().default(600),

  /** Custom install command to run after init. */
  customInstallCmd: z.string().nullable().default(null),

  /** Directory to create symlinks of executables. If null, no symlinks are created. */
  extraSymlinkDir: z.string().nullable().default(null),

  /** List of executable names to symlink. Empty list means no symlinks. */
  extraSymlinkExecutables: z.array(z.string()).default([]),
});

export type RuntimeEnvConfig = z.infer<typeof RuntimeEnvConfigSchema>;
