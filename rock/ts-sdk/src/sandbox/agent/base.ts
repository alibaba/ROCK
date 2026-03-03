/**
 * Agent base classes
 */

import { initLogger } from '../../logger.js';
import type { RockAgentConfig } from './config.js';
import type { SandboxLike } from '../runtime_env/base.js';
import type { ModelService } from '../model_service/base.js';
import type { Observation } from '../../types/responses.js';

const logger = initLogger('rock.agent');

/**
 * Abstract Agent base class
 */
export abstract class Agent {
  protected _sandbox: SandboxLike;
  protected _modelService: ModelService | null = null;

  constructor(sandbox: SandboxLike) {
    this._sandbox = sandbox;
  }

  get sandbox(): SandboxLike {
    return this._sandbox;
  }

  get modelService(): ModelService | null {
    return this._modelService;
  }

  abstract install(config: RockAgentConfig): Promise<void>;
  abstract run(prompt: string): Promise<Observation>;
}

/**
 * DefaultAgent with common initialization and execution logic
 */
export class DefaultAgent extends Agent {
  protected _config: RockAgentConfig | null = null;
  protected _agentSession: string | null = null;

  get config(): RockAgentConfig | null {
    return this._config;
  }

  get agentSession(): string | null {
    return this._agentSession;
  }

  override get modelService(): ModelService | null {
    return this._modelService;
  }

  async install(config: RockAgentConfig): Promise<void> {
    this._config = config;
    this._agentSession = config.agentSession;

    const sandboxId = this._sandbox.sandboxId;
    logger.info(`[${sandboxId}] Starting agent initialization`);

    try {
      // Setup bash session
      await this._setupSession();

      // Execute pre-init commands
      await this._executeInitCommands(config.preInitCmds, 'pre-init');

      // Execute post-init commands
      await this._executeInitCommands(config.postInitCmds, 'post-init');

      logger.info(`[${sandboxId}] Agent initialization completed`);
    } catch (e) {
      const error = e as Error;
      logger.error(`[${sandboxId}] Agent initialization failed: ${error.message}`);
      throw error;
    }
  }

  async run(prompt: string): Promise<Observation> {
    if (!this._config) {
      throw new Error('Agent is not installed. Please call install() first.');
    }

    if (!this._config.runCmd) {
      throw new Error('runCmd is not configured');
    }

    const cmd = await this._createAgentRunCmd(prompt);
    return this._agentRun(cmd);
  }

  protected async _setupSession(): Promise<void> {
    if (!this._config) return;

    const sandboxId = this._sandbox.sandboxId;
    logger.info(`[${sandboxId}] Creating bash session: ${this._agentSession}`);

    await this._sandbox.createSession({
      session: this._agentSession!,
      envEnable: true,
      env: this._config.env,
    });

    logger.info(`[${sandboxId}] Bash session '${this._agentSession}' created successfully`);
  }

  protected async _executeInitCommands(
    cmdList: Array<{ command: string; timeoutSeconds: number }>,
    stepName: string
  ): Promise<void> {
    if (!cmdList || cmdList.length === 0) return;

    const sandboxId = this._sandbox.sandboxId;
    logger.info(`[${sandboxId}] ${stepName} started: Executing ${cmdList.length} commands`);

    for (let idx = 0; idx < cmdList.length; idx++) {
      const cmdConfig = cmdList[idx];
      if (!cmdConfig) continue;
      
      const command = cmdConfig.command;
      const timeout = cmdConfig.timeoutSeconds;

      logger.debug(
        `[${sandboxId}] Executing ${stepName} command ${idx + 1}/${cmdList.length}: ${command.substring(0, 100)}...`
      );

      const result = await this._sandbox.arun(`bash -c ${JSON.stringify(command)}`, {
        waitTimeout: timeout,
        mode: 'NOHUP',
      });

      if (result.exitCode !== 0) {
        throw new Error(
          `[${sandboxId}] ${stepName} command ${idx + 1} failed with exit code ${result.exitCode}: ${result.output.substring(0, 200)}`
        );
      }

      logger.debug(`[${sandboxId}] ${stepName} command ${idx + 1} completed successfully`);
    }

    logger.info(`[${sandboxId}] ${stepName} completed: Completed ${cmdList.length} commands`);
  }

  protected async _createAgentRunCmd(prompt: string): Promise<string> {
    if (!this._config || !this._config.runCmd) {
      throw new Error('runCmd is not configured');
    }

    // Replace {prompt} placeholder
    let runCmd = this._config.runCmd.replace(/{prompt}/g, JSON.stringify(prompt));

    // Skip wrap if configured
    if (this._config.skipWrapRunCmd) {
      return `bash -c ${JSON.stringify(runCmd)}`;
    }

    return `bash -c ${JSON.stringify(runCmd)}`;
  }

  protected async _agentRun(cmd: string): Promise<Observation> {
    const sandboxId = this._sandbox.sandboxId;

    try {
      const timestamp = Date.now().toString();
      const tmpFile = `/tmp/tmp_${timestamp}.out`;

      // Check if startNohupProcess is available
      if (!this._sandbox.startNohupProcess) {
        const msg = 'startNohupProcess method is not available on sandbox';
        return { output: msg, exitCode: 1, failureReason: msg, expectString: '' };
      }

      // Start nohup process and get PID
      const { pid, errorResponse } = await this._sandbox.startNohupProcess(cmd, tmpFile, this._agentSession!);

      if (errorResponse) {
        return errorResponse;
      }

      if (!pid) {
        const msg = 'Failed to submit command, nohup failed to extract PID';
        return { output: msg, exitCode: 1, failureReason: msg, expectString: '' };
      }

      logger.info(`[${sandboxId}] Agent process started with PID: ${pid}`);

      // Check if waitForProcessCompletion is available
      if (!this._sandbox.waitForProcessCompletion) {
        const msg = 'waitForProcessCompletion method is not available on sandbox';
        return { output: msg, exitCode: 1, failureReason: msg, expectString: '' };
      }

      // Wait for agent process to complete
      const { success, message } = await this._sandbox.waitForProcessCompletion(
        pid,
        this._agentSession!,
        this._config!.agentRunTimeout,
        this._config!.agentRunCheckInterval
      );

      // Check if handleNohupOutput is available
      if (!this._sandbox.handleNohupOutput) {
        const msg = 'handleNohupOutput method is not available on sandbox';
        return { output: msg, exitCode: 1, failureReason: msg, expectString: '' };
      }

      // Handle nohup output and return result
      const result = await this._sandbox.handleNohupOutput(tmpFile, this._agentSession!, success, message, false, null);

      return result;
    } catch (e) {
      const error = e as Error;
      const errorMsg = `Failed to execute nohup command: ${error.message}`;
      logger.error(`[${sandboxId}] ${errorMsg}`);
      return { output: errorMsg, exitCode: 1, failureReason: errorMsg, expectString: '' };
    }
  }
}
