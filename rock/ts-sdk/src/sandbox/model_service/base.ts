/**
 * ModelService - manages model service installation and lifecycle in sandbox
 *
 * This module provides functionality to install, start, stop, and manage
 * the model service within a sandboxed environment.
 */

import { z } from 'zod';
import { PythonRuntimeEnv, PythonRuntimeEnvConfigSchema } from '../runtime_env/python_runtime_env.js';
import { type RuntimeEnvId, type SandboxLike } from '../runtime_env/base.js';
import { envVars } from '../../env_vars.js';
import { initLogger } from '../../logger.js';

const logger = initLogger('rock.model_service');

/**
 * ModelService configuration schema
 */
export const ModelServiceConfigSchema = z.object({
  /** Whether to enable model service */
  enabled: z.boolean().default(false),

  /** Type of model service to start */
  type: z.string().default('local'),

  /** Command to install model service package */
  installCmd: z.string().default(envVars.ROCK_MODEL_SERVICE_INSTALL_CMD),

  /** Timeout for model service installation in seconds */
  installTimeout: z.number().positive().default(300),

  /** Runtime environment configuration for the model service */
  runtimeEnvConfig: PythonRuntimeEnvConfigSchema.default({ type: 'python', version: 'default' }),

  /** Command to start model service with type placeholder */
  startCmd: z.string().default('rock model-service start --type ${type}'),

  /** Command to stop model service */
  stopCmd: z.string().default('rock model-service stop'),

  /** Command to create Rock config file */
  configIniCmd: z.string().default('mkdir -p ~/.rock && touch ~/.rock/config.ini'),

  /** Command to watch agent with pid placeholder */
  watchAgentCmd: z.string().default('rock model-service watch-agent --pid ${pid}'),

  /** Command to anti-call LLM with index and response_payload placeholders */
  antiCallLlmCmd: z.string().default(
    'rock model-service anti-call-llm --index ${index} --response ${response_payload}'
  ),

  /** Command to anti-call LLM with only index placeholder */
  antiCallLlmCmdNoResponse: z.string().default('rock model-service anti-call-llm --index ${index}'),

  /** Path for logging directory */
  loggingPath: z.string().default('/data/logs'),

  /** Name of the log file */
  loggingFileName: z.string().default('model_service.log'),
});

/**
 * ModelService configuration type
 */
export type ModelServiceConfig = z.infer<typeof ModelServiceConfigSchema>;

/**
 * ModelService - manages model service installation and lifecycle in sandbox
 *
 * This class handles model service installation, startup, and agent management
 * within a sandboxed environment.
 *
 * Note:
 * Caller is responsible for ensuring proper sequencing of install/start/stop operations.
 */
export class ModelService {
  private _sandbox: SandboxLike;
  private _config: ModelServiceConfig;
  private _runtimeEnv: PythonRuntimeEnv | null = null;

  private _isInstalled = false;
  private _isStarted = false;

  constructor(
    sandbox: SandboxLike & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnvLike> },
    config: ModelServiceConfig
  ) {
    this._sandbox = sandbox;
    this._config = config;
  }

  /** Whether the model service has been installed */
  get isInstalled(): boolean {
    return this._isInstalled;
  }

  /** Whether the model service has been started */
  get isStarted(): boolean {
    return this._isStarted;
  }

  /**
   * Install model service in the sandbox.
   *
   * Performs the following installation steps:
   * 1. Create and initialize Python runtime environment (via RuntimeEnv).
   * 2. Install model service package.
   */
  async install(): Promise<void> {
    // Validate runtime config is Python
    if (this._config.runtimeEnvConfig.type !== 'python') {
      throw new Error('ModelService requires a Python runtime environment');
    }

    // Parse and validate the runtime config
    const runtimeConfigResult = PythonRuntimeEnvConfigSchema.safeParse(this._config.runtimeEnvConfig);
    if (!runtimeConfigResult.success) {
      throw new Error(`Invalid runtime config: ${runtimeConfigResult.error.message}`);
    }

    // Create Python runtime environment
    this._runtimeEnv = new PythonRuntimeEnv(
      this._sandbox as SandboxLike & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnvLike> },
      runtimeConfigResult.data
    );

    // Initialize the runtime (installs Python)
    await this._runtimeEnv.init();

    // Create rock config
    await this._createRockConfig();

    // Install model service package
    await this._installModelService();

    this._isInstalled = true;
  }

  private async _createRockConfig(): Promise<void> {
    if (!this._runtimeEnv) {
      throw new Error('Runtime environment not initialized');
    }
    await this._runtimeEnv.run(this._config.configIniCmd);
  }

  private async _installModelService(): Promise<void> {
    if (!this._runtimeEnv) {
      throw new Error('Runtime environment not initialized');
    }

    const installCmd = `cd ${this._runtimeEnv.workdir} && ${this._config.installCmd}`;
    await this._runtimeEnv.run(installCmd, {
      waitTimeout: this._config.installTimeout,
      errorMsg: 'Model service installation failed',
    });
  }

  /**
   * Start the model service in the sandbox.
   *
   * Starts the service with configured logging settings.
   */
  async start(): Promise<void> {
    if (!this._isInstalled) {
      throw new Error(
        `[${this._sandbox.sandboxId}] Cannot start model service: ModelService has not been installed yet. ` +
          'Please call install() first.'
      );
    }

    if (!this._runtimeEnv) {
      throw new Error('Runtime environment not initialized');
    }

    const startCmd = this._config.startCmd.replace(/\$\{type\}/g, this._config.type);
    const bashStartCmd =
      `export ROCK_LOGGING_PATH=${this._config.loggingPath} && ` +
      `export ROCK_LOGGING_FILE_NAME=${this._config.loggingFileName} && ` +
      `${this._config.stopCmd} && ` +
      startCmd;

    logger.debug(`[${this._sandbox.sandboxId}] Model service Start command: ${bashStartCmd}`);

    await this._runtimeEnv.run(bashStartCmd);

    this._isStarted = true;
  }

  /**
   * Stop the model service.
   */
  async stop(): Promise<void> {
    if (!this._isStarted) {
      logger.warn(
        `[${this._sandbox.sandboxId}] Model service is not running, skipping stop operation. is_started=${this._isStarted}`
      );
      return;
    }

    if (!this._runtimeEnv) {
      throw new Error('Runtime environment not initialized');
    }

    await this._runtimeEnv.run(this._config.stopCmd);

    this._isStarted = false;
  }

  /**
   * Watch agent process with the specified PID.
   */
  async watchAgent(pid: string): Promise<void> {
    if (!this._isStarted) {
      throw new Error(
        `[${this._sandbox.sandboxId}] Cannot watch agent: ModelService is not started. Please call start() first.`
      );
    }

    if (!this._runtimeEnv) {
      throw new Error('Runtime environment not initialized');
    }

    const watchCmd = this._config.watchAgentCmd.replace(/\$\{pid\}/g, pid);
    logger.debug(
      `[${this._sandbox.sandboxId}] Model service watch agent with pid=${pid}, cmd: ${watchCmd}`
    );

    await this._runtimeEnv.run(watchCmd);
  }

  /**
   * Execute anti-call LLM command.
   */
  async antiCallLlm(
    index: number,
    responsePayload?: string,
    callTimeout = 600
  ): Promise<string> {
    if (!this._isStarted) {
      throw new Error(
        `[${this._sandbox.sandboxId}] Cannot execute anti-call LLM: ModelService is not started. Please call start() first.`
      );
    }

    if (!this._runtimeEnv) {
      throw new Error('Runtime environment not initialized');
    }

    logger.info(
      `[${this._sandbox.sandboxId}] Executing anti-call LLM: index=${index}, ` +
        `has_response=${responsePayload !== undefined}, timeout=${callTimeout}s`
    );

    let cmd: string;
    if (responsePayload !== undefined) {
      const escapedPayload = `'${responsePayload.replace(/'/g, "'\\''")}'`;
      cmd = this._config.antiCallLlmCmd
        .replace(/\$\{index\}/g, String(index))
        .replace(/\$\{response_payload\}/g, escapedPayload);
    } else {
      cmd = this._config.antiCallLlmCmdNoResponse.replace(/\$\{index\}/g, String(index));
    }

    const bashCmd = this._runtimeEnv.wrappedCmd(cmd);
    logger.debug(`[${this._sandbox.sandboxId}] Executing command: ${bashCmd}`);

    const result = await this._sandbox.arun(bashCmd, {
      mode: 'nohup',
      waitTimeout: callTimeout,
      waitInterval: 3,
    });

    if (result.exitCode !== 0) {
      throw new Error(`Anti-call LLM command failed: ${result.output}`);
    }

    return result.output;
  }
}

/**
 * Minimal RuntimeEnv interface for type checking
 */
interface RuntimeEnvLike {
  init(): Promise<void>;
  run(cmd: string, mode?: string, waitTimeout?: number, errorMsg?: string): Promise<{ output: string; exitCode?: number }>;
  workdir: string;
  wrappedCmd(cmd: string): string;
}
