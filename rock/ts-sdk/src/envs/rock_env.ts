/**
 * RockEnv - Gym-style environment interface
 */

import axios, { AxiosInstance } from 'axios';
import { envVars } from '../env_vars.js';
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
  private client: AxiosInstance;

  constructor(config: RockEnvConfig) {
    this.envId = config.envId;
    this.client = axios.create({
      baseURL: envVars.ROCK_BASE_URL,
      timeout: 300000,
      headers: { 'Content-Type': 'application/json' },
    });

    try {
      this.initializeEnvironment();
    } catch (e) {
      throw new Error(`Failed to initialize environment: ${e}`);
    }
  }

  /**
   * Initialize environment instance
   */
  private initializeEnvironment(): void {
    logger.debug(`Initializing environment: ${this.envId}`);
    // This would normally call the admin API
    // For now, we'll leave the implementation as a stub
    // that can be filled in based on actual API requirements
  }

  /**
   * Execute an action step
   *
   * @param action - Action ID to execute
   * @returns Tuple containing observation, reward, terminated, truncated, info
   */
  async step(action: string | number): Promise<StepResult> {
    this.ensureNotClosed();

    const params = {
      sandbox_id: this.sandboxId,
      action,
    };

    try {
      const response = await this.client.post(
        '/apis/v1/envs/gem/step',
        params
      );
      return this.parseStepResult(response.data);
    } catch (e) {
      throw new Error(`Failed to execute step with action ${action}: ${e}`);
    }
  }

  /**
   * Reset environment to initial state
   *
   * @param seed - Optional random seed
   * @returns Tuple containing initial observation and info
   */
  async reset(seed?: number): Promise<ResetResult> {
    this.ensureNotClosed();

    const params: Record<string, unknown> = { sandbox_id: this.sandboxId };
    if (seed !== undefined) {
      params.seed = seed;
    }

    try {
      const response = await this.client.post(
        '/apis/v1/envs/gem/reset',
        params
      );
      return this.parseResetResult(response.data);
    } catch (e) {
      throw new Error(`Failed to reset environment: ${e}`);
    }
  }

  /**
   * Close environment and clean up resources
   */
  async close(): Promise<void> {
    if (this.isClosed || !this.sandboxId) {
      return;
    }

    try {
      await this.client.post('/apis/v1/envs/gem/close', {
        sandbox_id: this.sandboxId,
      });
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
  private parseStepResult(data: Record<string, unknown>): StepResult {
    return [
      data.observation,
      data.reward as number,
      data.terminated as boolean,
      data.truncated as boolean,
      data.info as Record<string, unknown>,
    ];
  }

  /**
   * Parse reset result from API response
   */
  private parseResetResult(data: Record<string, unknown>): ResetResult {
    return [data.observation, data.info as Record<string, unknown>];
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
