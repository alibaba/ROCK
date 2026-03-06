/**
 * RockEnv - Gym-style environment interface
 */

import { envVars } from '../env_vars.js';
import { HttpUtils } from '../utils/http.js';
import { initLogger } from '../logger.js';

const logger = initLogger('rock.envs');

/**
 * Step result tuple type
 */
export type StepResult = [
  observation: unknown,
  reward: number,
  terminated: boolean,
  truncated: boolean,
  info: Record<string, unknown>
];

/**
 * Reset result tuple type
 */
export type ResetResult = [observation: unknown, info: Record<string, unknown>];

/**
 * RockEnv configuration
 */
export interface RockEnvConfig {
  envId: string;
}

/**
 * RockEnv - Gym-style environment for ROCK
 */
export class RockEnv {
  private readonly envId: string;
  private sandboxId: string | null = null;
  private isClosed = false;

  private constructor(config: RockEnvConfig) {
    this.envId = config.envId;
  }

  /**
   * Create and initialize a RockEnv instance
   *
   * @param config - Environment configuration
   * @returns Initialized RockEnv instance
   */
  static async create(config: RockEnvConfig): Promise<RockEnv> {
    const env = new RockEnv(config);
    try {
      await env.initializeEnvironment();
    } catch (e) {
      throw new Error(`Failed to initialize environment: ${e}`);
    }
    return env;
  }

  /**
   * Get the sandbox ID
   */
  getSandboxId(): string | null {
    return this.sandboxId;
  }

  /**
   * Initialize environment instance
   */
  private async initializeEnvironment(): Promise<void> {
    logger.debug(`Initializing environment: ${this.envId}`);
    const response = await HttpUtils.post<{ sandboxId: string }>(
      `${envVars.ROCK_BASE_URL}/apis/v1/envs/gem/make`,
      { 'Content-Type': 'application/json' },
      { envId: this.envId }
    );

    this.sandboxId = response.result?.sandboxId ?? null;
    if (!this.sandboxId) {
      throw new Error('Failed to get environment instance ID');
    }
  }

  /**
   * Execute an action step
   *
   * @param action - Action ID to execute
   * @returns Tuple containing observation, reward, terminated, truncated, info
   */
  async step(action: string | number): Promise<StepResult> {
    this.ensureNotClosed();

    const response = await HttpUtils.post<{
      observation: unknown;
      reward: number;
      terminated: boolean;
      truncated: boolean;
      info: Record<string, unknown>;
    }>(
      `${envVars.ROCK_BASE_URL}/apis/v1/envs/gem/step`,
      { 'Content-Type': 'application/json' },
      { sandboxId: this.sandboxId, action }
    );

    return this.parseStepResult(response.result);
  }

  /**
   * Reset environment to initial state
   *
   * @param seed - Optional random seed
   * @returns Tuple containing initial observation and info
   */
  async reset(seed?: number): Promise<ResetResult> {
    this.ensureNotClosed();

    const params: Record<string, unknown> = { sandboxId: this.sandboxId };
    if (seed !== undefined) {
      params.seed = seed;
    }

    const response = await HttpUtils.post<{
      observation: unknown;
      info: Record<string, unknown>;
    }>(
      `${envVars.ROCK_BASE_URL}/apis/v1/envs/gem/reset`,
      { 'Content-Type': 'application/json' },
      params
    );

    return this.parseResetResult(response.result);
  }

  /**
   * Close environment and clean up resources
   */
  async close(): Promise<void> {
    if (this.isClosed || !this.sandboxId) {
      return;
    }

    try {
      await HttpUtils.post(
        `${envVars.ROCK_BASE_URL}/apis/v1/envs/gem/close`,
        { 'Content-Type': 'application/json' },
        { sandboxId: this.sandboxId }
      );
    } catch (e) {
      throw new Error(`Failed to close environment: ${e}`);
    } finally {
      this.isClosed = true;
      this.sandboxId = null;
    }
  }

  /**
   * Parse step result from API response
   */
  private parseStepResult(
    data:
      | {
          observation: unknown;
          reward: number;
          terminated: boolean;
          truncated: boolean;
          info: Record<string, unknown>;
        }
      | undefined
  ): StepResult {
    if (!data) {
      throw new Error('Invalid step result: no data');
    }
    return [
      data.observation,
      data.reward,
      data.terminated,
      data.truncated,
      data.info,
    ];
  }

  /**
   * Parse reset result from API response
   */
  private parseResetResult(
    data: { observation: unknown; info: Record<string, unknown> } | undefined
  ): ResetResult {
    if (!data) {
      throw new Error('Invalid reset result: no data');
    }
    return [data.observation, data.info];
  }

  /**
   * Ensure environment is not closed
   */
  private ensureNotClosed(): void {
    if (this.isClosed) {
      throw new Error('Environment is closed');
    }
  }
}