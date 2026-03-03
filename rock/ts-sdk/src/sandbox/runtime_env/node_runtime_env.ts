/**
 * Node.js runtime environment configuration and implementation
 */

import { z } from 'zod';
import { RuntimeEnvConfigSchema } from './config.js';
import { RuntimeEnv, type RuntimeEnvId, type SandboxLike } from './base.js';
import { envVars } from '../../env_vars.js';

/** Default Node.js version */
export const NODE_DEFAULT_VERSION = '22.18.0';

/**
 * Configuration for Node.js runtime environment.
 */
export const NodeRuntimeEnvConfigSchema = RuntimeEnvConfigSchema.extend({
  /** Runtime type discriminator. Must be 'node'. */
  type: z.literal('node').default('node'),

  /** Node.js version. Use "default" for 22.18.0. */
  version: z.enum(['22.18.0', 'default']).default('default'),

  /** NPM registry URL. If set, will run 'npm config set registry <url>' during init. */
  npmRegistry: z.string().nullable().default(null),

  /** List of Node.js executables to symlink. */
  extraSymlinkExecutables: z.array(z.string()).default(['node', 'npm', 'npx']),
});

/**
 * Node runtime environment configuration type
 */
export type NodeRuntimeEnvConfig = z.infer<typeof NodeRuntimeEnvConfigSchema>;

/**
 * Node runtime environment
 *
 * Provides Node.js runtime with npm package management.
 * Supports Node.js 22.18.0.
 *
 * @example
 * ```typescript
 * const config: NodeRuntimeEnvConfig = {
 *   version: 'default',
 *   npmRegistry: 'https://registry.npmmirror.com',
 * };
 * const env = new NodeRuntimeEnv(sandbox, config);
 * await env.init();
 * await env.run('node --version');
 * ```
 */
export class NodeRuntimeEnv extends RuntimeEnv {
  readonly runtimeEnvType = 'node';

  private _npmRegistry: string | null | undefined;

  constructor(
    sandbox: SandboxLike & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> },
    config: NodeRuntimeEnvConfig
  ) {
    if (config.version !== 'default' && config.version !== NODE_DEFAULT_VERSION) {
      throw new Error(
        `Unsupported Node version: ${config.version}. Only ${NODE_DEFAULT_VERSION} is supported right now.`
      );
    }

    super(sandbox, config);

    this._npmRegistry = config.npmRegistry;
  }

  protected _getInstallCmd(): string {
    return envVars.ROCK_RTENV_NODE_V22180_INSTALL_CMD;
  }

  protected override async _postInit(): Promise<void> {
    // Step 1: validate node exists
    await this._validateNode();

    // Step 2: configure npm registry if specified
    if (this._npmRegistry) {
      await this._configureNpmRegistry();
    }
  }

  private async _validateNode(): Promise<void> {
    await this.run('test -x node');
  }

  private async _configureNpmRegistry(): Promise<void> {
    const escapedRegistry = this._npmRegistry!.replace(/'/g, "'\\''");
    await this.run(`npm config set registry '${escapedRegistry}'`);
  }
}