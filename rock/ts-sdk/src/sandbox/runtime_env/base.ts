import { randomUUID } from 'crypto';
import { initLogger } from '../../logger.js';
import { RuntimeEnvConfig } from './config.js';
import type { Observation } from '../../types/responses.js';

const logger = initLogger('rock.sandbox.runtime_env');

/** Unique identifier for a RuntimeEnv instance */
export type RuntimeEnvId = string;

/** Create a new unique RuntimeEnvId */
export function createRuntimeEnvId(): RuntimeEnvId {
  return randomUUID().slice(0, 8);
}

/** Registry for RuntimeEnv subclasses */
const RUNTIME_ENV_REGISTRY: Record<string, typeof RuntimeEnv> = {};

/**
 * Interface for Sandbox with methods needed by RuntimeEnv
 */
export interface SandboxLike {
  sandboxId: string;
  runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv>;
  createSession(request: { session: string; envEnable: boolean; env?: Record<string, string> }): Promise<unknown>;
  arun(
    cmd: string,
    options?: {
      session?: string;
      mode?: 'normal' | 'nohup' | 'NOHUP';
      waitTimeout?: number;
      waitInterval?: number;
    }
  ): Promise<Observation>;
  startNohupProcess?(
    cmd: string,
    tmpFile: string,
    session: string
  ): Promise<{ pid: number | null; errorResponse: Observation | null }>;
  waitForProcessCompletion?(
    pid: number,
    session: string,
    waitTimeout: number,
    waitInterval: number
  ): Promise<{ success: boolean; message: string }>;
  handleNohupOutput?(
    tmpFile: string,
    session: string,
    success: boolean,
    message: string,
    ignoreOutput: boolean,
    responseLimitedBytes: number | null
  ): Promise<Observation>;
}

/**
 * Runtime environment (e.g., Python/Node).
 *
 * Each RuntimeEnv is identified by (type, version) tuple and is managed by Sandbox.runtimeEnvs.
 * workdir is auto-generated as: /tmp/rock-runtime-envs/{type}/{version}/{runtimeEnvId}
 * session is auto-generated as: runtime-env-{type}-{version}-{runtimeEnvId}
 *
 * @example
 * ```typescript
 * const env = await RuntimeEnv.create(sandbox, config);
 * await env.run("python --version");
 * ```
 */
export abstract class RuntimeEnv {
  /** Registry for subclasses */
  protected static registry: Record<string, typeof RuntimeEnv> = RUNTIME_ENV_REGISTRY;

  /** Runtime type discriminator - must be defined by subclass */
  abstract readonly runtimeEnvType: string;

  /** Sandbox instance */
  protected _sandbox: SandboxLike;

  /** Configuration */
  protected _config: RuntimeEnvConfig;

  /** Version */
  protected _version: string;

  /** Environment variables */
  protected _env: Record<string, string>;

  /** Install timeout in seconds */
  protected _installTimeout: number;

  /** Custom install command */
  protected _customInstallCmd: string | null;

  /** Extra symlink directory */
  protected _extraSymlinkDir: string | null;

  /** Extra symlink executables */
  protected _extraSymlinkExecutables: string[];

  /** Unique ID for this runtime env instance */
  protected _runtimeEnvId: RuntimeEnvId;

  /** Working directory */
  protected _workdir: string;

  /** Session name */
  protected _session: string;

  /** Whether the runtime has been initialized */
  protected _initialized: boolean = false;

  /** Whether the session is ready */
  protected _sessionReady: boolean = false;

  constructor(sandbox: SandboxLike, config: RuntimeEnvConfig) {
    this._sandbox = sandbox;
    this._config = config;

    // Extract values from config
    this._version = config.version;
    this._env = config.env;
    this._installTimeout = config.installTimeout;
    this._customInstallCmd = config.customInstallCmd;
    this._extraSymlinkDir = config.extraSymlinkDir;
    this._extraSymlinkExecutables = config.extraSymlinkExecutables;

    // Unique ID for this runtime env instance
    this._runtimeEnvId = createRuntimeEnvId();

    // Generate workdir and session
    const versionStr = this._version || 'default'; // avoid empty version
    this._workdir = `/tmp/rock-runtime-envs/${config.type}/${versionStr}/${this._runtimeEnvId}`;
    this._session = `runtime-env-${config.type}-${versionStr}-${this._runtimeEnvId}`;
  }

  /** Whether the runtime has been initialized */
  get initialized(): boolean {
    return this._initialized;
  }

  /** Unique ID for this runtime env instance */
  get runtimeEnvId(): RuntimeEnvId {
    return this._runtimeEnvId;
  }

  /** Working directory for this runtime env instance */
  get workdir(): string {
    return this._workdir;
  }

  /** Binary directory for this runtime env instance */
  get binDir(): string {
    return `${this._workdir}/runtime-env/bin`;
  }

  /**
   * Initialize the runtime environment.
   * This method performs installation and validation.
   * It is idempotent: calling multiple times only initializes once.
   */
  async init(): Promise<void> {
    if (this._initialized) {
      return;
    }

    // Common setup: ensure session and workdir
    await this._ensureSession();
    await this._ensureWorkdir();

    // Install runtime and then do additional initialization
    await this._installRuntime();
    await this._postInit();

    // Execute custom install command after _postInit
    if (this._customInstallCmd) {
      await this._doCustomInstall();
    }

    // Create symlinks for executables
    await this._createSysPathLinks();

    this._initialized = true;
  }

  /**
   * Run a command under this runtime
   */
  async run(
    cmd: string,
    options: {
      mode?: 'normal' | 'nohup';
      waitTimeout?: number;
      errorMsg?: string;
    } = {}
  ): Promise<Observation> {
    const { mode = 'nohup', waitTimeout = 600, errorMsg = 'runtime env command failed' } = options;

    await this._ensureSession();
    const wrapped = this.wrappedCmd(cmd, true);

    logger.debug(`[${this._sandbox.sandboxId}] RuntimeEnv run cmd: ${wrapped}`);

    const result = await this._sandbox.arun(wrapped, {
      session: this._session,
      mode,
      waitTimeout,
    });

    // If exit_code is not 0, raise an exception to trigger retry
    if (result.exitCode !== undefined && result.exitCode !== 0) {
      throw new Error(`${errorMsg} with exit code: ${result.exitCode}, output: ${result.output}`);
    }
    return result;
  }

  /**
   * Wrap command with PATH export.
   * Always wrap with bash -c to ensure it only affects current cmd.
   * Default prepend=true to give current runtime_env highest priority.
   */
  wrappedCmd(cmd: string, prepend: boolean = true): string {
    const binDir = this.binDir;
    let wrapped: string;
    if (prepend) {
      wrapped = `export PATH='${binDir}':$PATH && ${cmd}`;
    } else {
      wrapped = `export PATH=$PATH:'${binDir}' && ${cmd}`;
    }
    return `bash -c '${wrapped.replace(/'/g, "'\"'\"'")}'`;
  }

  /**
   * Ensure runtime env session exists. Safe to call multiple times.
   */
  protected async _ensureSession(): Promise<void> {
    if (this._sessionReady) {
      return;
    }

    await this._sandbox.createSession({
      session: this._session,
      envEnable: true,
      env: this._env,
    });
    this._sessionReady = true;
  }

  /**
   * Create workdir for runtime environment.
   */
  protected async _ensureWorkdir(): Promise<void> {
    const result = await this._sandbox.arun(`mkdir -p ${this._workdir}`, {
      session: this._session,
    });
    if (result.exitCode !== undefined && result.exitCode !== 0) {
      throw new Error(`Failed to create workdir: ${this._workdir}, exit_code: ${result.exitCode}`);
    }
  }

  /**
   * Get installation command for this runtime environment.
   */
  protected abstract _getInstallCmd(): string;

  /**
   * Install the runtime environment.
   */
  protected async _installRuntime(): Promise<void> {
    const installCmd = `cd '${this._workdir}' && ${this._getInstallCmd()}`;
    const wrappedCmd = `bash -c '${installCmd.replace(/'/g, "'\"'\"'")}'`;

    const result = await this._sandbox.arun(wrappedCmd, {
      session: this._session,
      mode: 'nohup',
      waitTimeout: this._installTimeout,
    });

    if (result.exitCode !== undefined && result.exitCode !== 0) {
      throw new Error(
        `${this.runtimeEnvType} runtime installation failed with exit code: ${result.exitCode}, output: ${result.output}`
      );
    }
  }

  /**
   * Additional initialization after runtime installation.
   * Override in subclasses.
   */
  protected async _postInit(): Promise<void> {
    // Default: no additional initialization
  }

  /**
   * Execute custom install command after _postInit.
   */
  protected async _doCustomInstall(): Promise<void> {
    if (!this._customInstallCmd) {
      return;
    }
    await this.run(this._customInstallCmd, {
      waitTimeout: this._installTimeout,
      errorMsg: 'custom_install_cmd failed',
    });
  }

  /**
   * Create symlinks in target directory for executables.
   */
  protected async _createSysPathLinks(): Promise<void> {
    if (this._extraSymlinkDir === null) {
      return;
    }
    if (this._extraSymlinkExecutables.length === 0) {
      return;
    }

    // Build a single command with all symlinks
    const links = this._extraSymlinkExecutables
      .map((exe) => `ln -sf '${this.binDir}/${exe}' '${this._extraSymlinkDir}/${exe}'`)
      .join(' && ');

    await this.run(links);
  }
}
