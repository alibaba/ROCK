/**
 * Python runtime environment configuration and implementation
 */

import { z } from 'zod';
import { RuntimeEnvConfigSchema } from './config.js';
import { RuntimeEnv, type RuntimeEnvId, type SandboxLike } from './base.js';
import { envVars } from '../../env_vars.js';

/**
 * Python runtime environment configuration schema
 */
export const PythonRuntimeEnvConfigSchema = RuntimeEnvConfigSchema.extend({
  /** Runtime type discriminator. Must be 'python'. */
  type: z.literal('python').default('python'),

  /** Python version. Use "default" for 3.11. */
  version: z.enum(['3.11', '3.12', 'default']).default('default'),

  /**
   * Pip packages to install.
   * Can be:
   * - string[]: List of package names to install
   * - string: Path to requirements.txt file
   * - null: No packages to install
   */
  pip: z.union([z.array(z.string()), z.string()]).nullable().default(null),

  /** Pip index URL for package installation. If set, will use this mirror. */
  pipIndexUrl: z.string().nullable().default(null),

  /** List of Python executables to symlink. */
  extraSymlinkExecutables: z.array(z.string()).default(['python', 'python3', 'pip', 'pip3']),
});

/**
 * Python runtime environment configuration type
 */
export type PythonRuntimeEnvConfig = z.infer<typeof PythonRuntimeEnvConfigSchema>;

/**
 * Get default pip index URL from environment variable.
 */
export function getDefaultPipIndexUrl(): string {
  return envVars.ROCK_PIP_INDEX_URL;
}

/**
 * Python runtime environment
 *
 * Provides Python runtime with pip package management.
 * Supports Python 3.11 and 3.12 versions.
 *
 * @example
 * ```typescript
 * const config: PythonRuntimeEnvConfig = {
 *   version: 'default',
 *   pip: ['langchain', 'langchain-openai'],
 *   pipIndexUrl: 'https://mirrors.aliyun.com/pypi/simple/',
 * };
 * const env = new PythonRuntimeEnv(sandbox, config);
 * await env.init();
 * await env.run('python --version');
 * ```
 */
export class PythonRuntimeEnv extends RuntimeEnv {
  readonly runtimeEnvType = 'python';

  private _pip: string[] | string | null | undefined;
  private _pipIndexUrl: string | null | undefined;

  constructor(
    sandbox: SandboxLike & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> },
    config: PythonRuntimeEnvConfig
  ) {
    // Validate version early
    if (config.version !== '3.11' && config.version !== '3.12' && config.version !== 'default') {
      throw new Error(
        `Unsupported Python version: ${config.version}. Supported versions: 3.11, 3.12, default`
      );
    }

    super(sandbox, config);

    this._pip = config.pip;
    this._pipIndexUrl = config.pipIndexUrl;
  }

  protected _getInstallCmd(): string {
    const version = this._version;
    if (version === '3.11' || version === 'default') {
      return envVars.ROCK_RTENV_PYTHON_V31114_INSTALL_CMD;
    }
    return envVars.ROCK_RTENV_PYTHON_V31212_INSTALL_CMD;
  }

  protected override async _postInit(): Promise<void> {
    // Step 1: validate python exists
    await this._validatePython();

    // Step 2: configure pip index url if specified
    if (this._pipIndexUrl) {
      await this._configurePip();
    }

    // Step 3: install pip packages if specified
    if (this._pip) {
      await this._installPip();
    }
  }

  private async _validatePython(): Promise<void> {
    await this.run('test -x python');
  }

  private async _configurePip(): Promise<void> {
    const escapedUrl = this._pipIndexUrl!.replace(/'/g, "'\\''");
    await this.run(`pip config set global.index-url '${escapedUrl}'`);
  }

  private async _installPip(): Promise<void> {
    if (!this._pip) {
      return;
    }

    if (typeof this._pip === 'string') {
      // Treat as requirements.txt path - note: for remote sandbox, local file upload
      // would need to be handled differently. For now, we assume the file is already
      // in the sandbox or use the array form.
      // This is a simplified implementation - the Python SDK handles local file upload.
      await this.run(`pip install -r '${this._pip.replace(/'/g, "'\\''")}'`);
    } else {
      // Treat as list of packages
      const packages = this._pip.map((pkg) => `'${pkg.replace(/'/g, "'\\''")}'`).join(' ');
      await this.run(`pip install ${packages}`);
    }
  }
}