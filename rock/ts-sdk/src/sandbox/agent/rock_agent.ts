/**
 * RockAgent - Full agent implementation for sandbox environments.
 *
 * Responsibilities:
 * - Manage RuntimeEnv installation and initialization (Python environments)
 * - Upload and provision working directory from local to sandbox
 * - Execute pre/post initialization commands
 * - Provide unified agent run entry with bash wrapper
 * - Support optional ModelService integration for LLM support
 *
 * Initialization flow:
 * 1. Provision working directory (upload local dir to sandbox)
 * 2. Setup bash session with environment variables
 * 3. Execute pre-init commands
 * 4. Parallel: RuntimeEnv init + ModelService install (if configured)
 * 5. Execute post-init commands
 */

import { createHash } from 'crypto';
import { initLogger } from '../../logger.js';
import { Agent } from './base.js';
import {
  RockAgentConfigSchema,
  loadRockAgentConfigFromYaml,
  type RockAgentConfig,
  type AgentBashCommand,
} from './config.js';
import { Deploy } from '../deploy.js';
import { RuntimeEnv, type SandboxLike } from '../runtime_env/base.js';
import { PythonRuntimeEnv, PythonRuntimeEnvConfigSchema } from '../runtime_env/python_runtime_env.js';
import { NodeRuntimeEnv, NodeRuntimeEnvConfigSchema } from '../runtime_env/node_runtime_env.js';
import { ModelService } from '../model_service/base.js';
import type { ModelServiceConfig } from '../model_service/base.js';
import type { Sandbox } from '../client.js';
import type { Observation } from '../../types/responses.js';

const logger = initLogger('rock.agent');

/**
 * Shell-quote a string for safe bash usage.
 * Wraps the string in single quotes, escaping any embedded single quotes.
 */
function shellQuote(s: string): string {
  return `'${s.replace(/'/g, "'\"'\"'")}'`;
}

/** Default Python runtime env config used when none specified */
const DEFAULT_RUNTIME_ENV_CONFIG = { type: 'python' as const, version: 'default' as const };

/**
 * Create a RuntimeEnv instance from config and initialize it.
 *
 * Dispatches on runtime_env_config.type to create the correct subclass.
 * Auto-registers the instance into sandbox.runtimeEnvs.
 */
async function createRuntimeEnv(
  sandbox: Sandbox,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  runtimeEnvConfig: Record<string, any>
): Promise<RuntimeEnv> {
  const runtimeType = runtimeEnvConfig?.type || 'python';

  let env: RuntimeEnv;
  if (runtimeType === 'python') {
    const config = PythonRuntimeEnvConfigSchema.parse({
      ...DEFAULT_RUNTIME_ENV_CONFIG,
      ...runtimeEnvConfig,
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    env = new PythonRuntimeEnv(sandbox as any, config);
  } else if (runtimeType === 'node') {
    const config = NodeRuntimeEnvConfigSchema.parse({
      ...runtimeEnvConfig,
      type: 'node',
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    env = new NodeRuntimeEnv(sandbox as any, config);
  } else {
    throw new Error(`Unsupported runtime type: ${runtimeType}`);
  }

  // Auto-register to sandbox.runtimeEnvs (matching Python: sandbox.runtime_envs[env._runtime_env_id] = env)
  sandbox.runtimeEnvs[env.runtimeEnvId] = env;

  await env.init();
  return env;
}

/**
 * RockAgent - Full agent with RuntimeEnv, Deploy, and ModelService integration.
 *
 * Extends the abstract Agent base class and provides a complete agent initialization
 * and execution lifecycle matching the Python RockAgent implementation.
 */
export class RockAgent extends Agent {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  override _sandbox: SandboxLike = null as any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  override _modelService: ModelService | null = null;
  private _deploy: Deploy;
  private _runtimeEnv: RuntimeEnv | null = null;
  private _config: RockAgentConfig | null = null;
  private _agentSession: string | null = null;

  constructor(sandbox: Sandbox) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    super(sandbox as any);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    this._sandbox = sandbox as any;
    this._deploy = sandbox.getDeploy();
  }

  // Accessor for Sandbox-specific methods not on SandboxLike
  private get s(): Sandbox { return this._sandbox as unknown as Sandbox; }

  get deploy(): Deploy {
    return this._deploy;
  }

  override get modelService(): ModelService | null {
    return this._modelService;
  }

  get runtimeEnv(): RuntimeEnv | null {
    return this._runtimeEnv;
  }

  get config(): RockAgentConfig | null {
    return this._config;
  }

  get agentSession(): string | null {
    return this._agentSession;
  }

  /**
   * Install and initialize RockAgent.
   *
   * Initialization flow:
   * 1. Provision working directory (if configured)
   * 2. Setup bash session
   * 3. Execute pre-init commands
   * 4. Parallel: RuntimeEnv init + ModelService install (if enabled)
   * 5. Execute post-init commands
   *
   * @param config - Either a path to a YAML config file or a RockAgentConfig object
   */
  async install(config: string | RockAgentConfig): Promise<void> {
    // Resolve config: string path or direct object
    if (typeof config === 'string') {
      this._config = loadRockAgentConfigFromYaml(config);
    } else {
      this._config = RockAgentConfigSchema.parse(config);
    }

    this._agentSession = this._config.agentSession;

    const sandboxId = this.s.getSandboxId();
    const startTime = Date.now();

    logger.info(`[${sandboxId}] Starting agent initialization`);

    try {
      // Step 1: Provision working directory (upload local dir to sandbox)
      if (this._config.workingDir) {
        await this._deploy.deployWorkingDir(this._config.workingDir);
      }

      // Step 2: Setup bash session
      await this._setupSession();

      // Step 3: Execute pre-init commands
      await this._executePreInit();

      // Step 4: Parallel tasks - RuntimeEnv init + ModelService install
      const tasks: Promise<void>[] = [this._doInit()];

      if (this._config.modelServiceConfig?.enabled) {
        tasks.push(this._initModelService());
      }

      await Promise.all(tasks);

      // Step 5: Execute post-init commands
      await this._executePostInit();

      const elapsed = ((Date.now() - startTime) / 1000).toFixed(2);
      logger.info(`[${sandboxId}] Agent initialization completed (elapsed: ${elapsed}s)`);
    } catch (e) {
      const elapsed = ((Date.now() - startTime) / 1000).toFixed(2);
      const error = e instanceof Error ? e : new Error(String(e));
      logger.error(
        `[${sandboxId}] Agent initialization failed - ${error.message} (elapsed: ${elapsed}s)`
      );
      throw error;
    }
  }

  /**
   * Execute agent with the given prompt.
   *
   * Formats the run_cmd with prompt/substitutions, wraps with bash -c,
   * and runs in nohup mode with optional ModelService monitoring.
   */
  async run(prompt: string): Promise<Observation> {
    if (!this._config) {
      throw new Error('Agent is not installed. Please call install() first.');
    }

    if (!this._config.runCmd) {
      throw new Error('runCmd is not configured');
    }

    const cmd = await this._createAgentRunCmd(prompt);
    return this._agentRun(cmd, this._agentSession!);
  }

  // ---- Private initialization methods ----

  /**
   * Initialize the runtime environment.
   *
   * Uses runtimeEnvConfig from the agent configuration.
   * Idempotent: calling multiple times only initializes once.
   */
  private async _doInit(): Promise<void> {
    if (this._runtimeEnv?.initialized) {
      const sandboxId = this.s.getSandboxId();
      logger.info(`[${sandboxId}] RuntimeEnv already initialized, skipping install`);
      return;
    }

    const runtimeConfig = this._config!.runtimeEnvConfig ?? DEFAULT_RUNTIME_ENV_CONFIG;
    this._runtimeEnv = await createRuntimeEnv(this.s, runtimeConfig);
  }

  /**
   * Create and configure the bash session for agent operations.
   */
  private async _setupSession(): Promise<void> {
    const sandboxId = this.s.getSandboxId();

    try {
      logger.info(`[${sandboxId}] Creating bash session: ${this._agentSession}`);

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      await (this._sandbox as any).createSession({
        session: this._agentSession!,
        envEnable: true,
        env: this._config!.env,
      });

      logger.info(
        `[${sandboxId}] Setup Session completed: Bash session '${this._agentSession}' created successfully`
      );
    } catch (e) {
      const error = e instanceof Error ? e : new Error(String(e));
      logger.error(`[${sandboxId}] Failed to setup session: ${error.message}`);
      throw error;
    }
  }

  private async _executePreInit(): Promise<void> {
    await this._executeInitCommands(this._config!.preInitCmds, 'pre-init');
  }

  private async _executePostInit(): Promise<void> {
    await this._executeInitCommands(this._config!.postInitCmds, 'post-init');
  }

  /**
   * Execute init-stage commands using nohup.
   *
   * Automatically performs deploy.format() to replace ${working_dir} placeholders.
   */
  private async _executeInitCommands(
    cmdList: AgentBashCommand[],
    stepName: string
  ): Promise<void> {
    const sandboxId = this.s.getSandboxId();

    if (!cmdList || cmdList.length === 0) {
      return;
    }

    try {
      logger.info(
        `[${sandboxId}] ${stepName} started: Executing ${cmdList.length} commands`
      );

      for (let idx = 0; idx < cmdList.length; idx++) {
        const cmdConfig = cmdList[idx];
        if (!cmdConfig) continue;

        let command = cmdConfig.command;
        const timeout = cmdConfig.timeoutSeconds;

        // Replace ${working_dir} placeholder via deploy.format()
        command = this._deploy.format(command);

        logger.debug(
          `[${sandboxId}] Executing ${stepName} command ${idx + 1}/${cmdList.length}: ` +
            `${command.substring(0, 100)}... (timeout: ${timeout}s)`
        );

        const result = await this._sandbox.arun(`bash -c ${shellQuote(command)}`, {
          waitTimeout: timeout,
          mode: 'nohup',
        });

        if (result.exitCode !== 0) {
          throw new Error(
            `[${sandboxId}] ${stepName} command ${idx + 1} failed with exit code ` +
              `${result.exitCode}: ${result.output?.substring(0, 200)}`
          );
        }

        logger.debug(
          `[${sandboxId}] ${stepName} command ${idx + 1} completed successfully`
        );
      }

      logger.info(
        `[${sandboxId}] ${stepName} completed: Completed ${cmdList.length} commands`
      );
    } catch (e) {
      const error = e instanceof Error ? e : new Error(String(e));
      logger.error(`[${sandboxId}] ${stepName} execution failed: ${error.message}`);
      throw error;
    }
  }

  // ---- ModelService ----

  /**
   * Initialize and start ModelService.
   *
   * If the sandbox already has a ModelService, reuses it instead of creating
   * a new one. Otherwise, creates a ModelService instance, executes installation,
   * and starts the service.
   */
  private async _initModelService(): Promise<void> {
    const sandboxId = this.s.getSandboxId();

    try {
      // Check if sandbox already has a ModelService
      if (this.s.modelService) {
        logger.info(`[${sandboxId}] Reusing existing ModelService from sandbox`);
        this._modelService = this.s.modelService as ModelService;
        // Ensure it's installed and started if not already
        if (!this._modelService.isInstalled) {
          await this._modelService.install();
        }
        await this._modelService.start();
        logger.info(`[${sandboxId}] ModelService reused successfully`);
        return;
      }

      logger.info(`[${sandboxId}] Initializing ModelService`);

      const modelServiceConfig = this._config!.modelServiceConfig as ModelServiceConfig;
      if (!modelServiceConfig) {
        logger.warn(`[${sandboxId}] ModelService enabled but config is null, skipping`);
        return;
      }

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      this._modelService = new ModelService(this._sandbox as any, modelServiceConfig);

      await this._modelService.install();
      await this._modelService.start();

      // Ensure one sandbox has just one model service
      this.s.modelService = this._modelService;

      logger.info(`[${sandboxId}] ModelService initialized and started successfully`);
    } catch (e) {
      const error = e instanceof Error ? e : new Error(String(e));
      logger.error(`[${sandboxId}] ModelService initialization failed: ${error.message}`);
      throw error;
    }
  }

  // ---- Command creation and execution ----

  /**
   * Create agent run command.
   *
   * Automatically performs deploy.format() to replace ${working_dir}, ${prompt},
   * and ${bin_dir} placeholders.
   *
   * @param prompt - The user prompt to substitute into {prompt} placeholder
   * @returns The complete command string ready for execution
   */
  private async _createAgentRunCmd(prompt: string): Promise<string> {
    // Get project_path from config or deploy.working_dir based on config
    let path: string | null = this._config!.projectPath;

    // If projectPath is not set, check whether to use deploy.workingDir as fallback
    if (path === null) {
      if (this._config!.useDeployWorkingDirAsFallback) {
        path = this._deploy.workingDir;
      }
      // else: path stays null, will run without cd
    }

    // Build bin_dir from runtime env
    const binDir = this._runtimeEnv?.binDir ?? '';

    // Format run_cmd, replacing ${working_dir}, ${bin_dir} and ${prompt}
    const runCmd = this._deploy.format(this._config!.runCmd!, {
      prompt: shellQuote(prompt),
      bin_dir: binDir,
    });

    // Skip wrap if configured - just run directly with bash -c
    let wrappedCmd: string;
    if (this._config!.skipWrapRunCmd) {
      wrappedCmd = `bash -c ${shellQuote(runCmd)}`;
    } else if (this._runtimeEnv) {
      wrappedCmd = this._runtimeEnv.wrappedCmd(runCmd);
    } else {
      wrappedCmd = `bash -c ${shellQuote(runCmd)}`;
    }

    // If path exists, add mkdir and cd
    if (path !== null) {
      const projectPath = shellQuote(path);
      const parts = [
        `mkdir -p ${projectPath}`,
        `cd ${projectPath}`,
        wrappedCmd,
      ];
      return parts.join(' && ');
    }

    // path is null, run command directly without cd
    return wrappedCmd;
  }

  /**
   * Execute agent command in nohup mode with optional ModelService watch.
   *
   * @param cmd - Command to execute
   * @param session - Bash session name
   * @returns Execution result with exit code and output
   */
  private async _agentRun(cmd: string, session: string): Promise<Observation> {
    const sandboxId = this.s.getSandboxId();

    try {
      const timestamp = createHash('sha256')
        .update(String(Date.now()))
        .digest('hex')
        .substring(0, 16);
      const tmpFile = `/tmp/tmp_${timestamp}.out`;

      // Start nohup process and get PID
      const { pid, errorResponse } = await this.s.startNohupProcess(cmd, tmpFile, session);

      if (errorResponse) {
        return errorResponse;
      }

      if (pid === null) {
        const msg = 'Failed to submit command, nohup failed to extract PID';
        return { output: msg, exitCode: 1, failureReason: msg, expectString: '' };
      }

      logger.info(`[${sandboxId}] Agent process started with PID: ${pid}`);

      // If ModelService is configured, monitor the process
      if (this._modelService) {
        try {
          logger.info(`[${sandboxId}] Starting ModelService watch-agent for pid ${pid}`);
          await this._modelService.watchAgent(String(pid));
          logger.info(`[${sandboxId}] ModelService watch-agent started successfully`);
        } catch (e) {
          const error = e instanceof Error ? e : new Error(String(e));
          logger.error(`[${sandboxId}] Failed to start watch-agent: ${error.message}`);
          throw error;
        }
      }

      // Wait for agent process to complete
      logger.debug(`[${sandboxId}] Waiting for agent process completion (pid=${pid})`);
      const { success, message } = await this.s.waitForProcessCompletion(
        pid,
        session,
        this._config!.agentRunTimeout,
        this._config!.agentRunCheckInterval
      );

      // Handle nohup output and return result
      const result = await this.s.handleNohupOutput(
        tmpFile,
        session,
        success,
        message,
        false,  // ignoreOutput
        null    // responseLimitedBytesInNohup
      );

      return result;
    } catch (e) {
      const error = e instanceof Error ? e : new Error(String(e));
      const errorMsg = `Failed to execute nohup command '${cmd}': ${error.message}`;
      logger.error(`[${sandboxId}] ${errorMsg}`);
      return { output: errorMsg, exitCode: 1, failureReason: errorMsg, expectString: '' };
    }
  }
}