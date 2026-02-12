/**
 * EnvHub client for communicating with EnvHub server
 */

import { HttpUtils } from '../utils/http.js';
import {
  EnvHubClientConfig,
  EnvHubClientConfigSchema,
  RockEnvInfo,
  createRockEnvInfo,
} from './schema.js';

/**
 * EnvHub error exception
 */
export class EnvHubError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'EnvHubError';
  }
}

/**
 * EnvHub client for communicating with EnvHub server
 */
export class EnvHubClient {
  private config: EnvHubClientConfig;
  private baseUrl: string;
  private headers: Record<string, string>;

  constructor(config?: Partial<EnvHubClientConfig>) {
    this.config = EnvHubClientConfigSchema.parse(config ?? {});
    this.baseUrl = this.config.baseUrl;
    this.headers = { 'Content-Type': 'application/json' };
  }

  /**
   * Register or update an environment
   */
  async register(options: {
    envName: string;
    image: string;
    owner?: string;
    description?: string;
    tags?: string[];
    extraSpec?: Record<string, unknown>;
  }): Promise<RockEnvInfo> {
    const url = `${this.baseUrl}/env/register`;
    const payload = {
      env_name: options.envName,
      image: options.image,
      owner: options.owner ?? '',
      description: options.description ?? '',
      tags: options.tags ?? [],
      extra_spec: options.extraSpec,
    };

    try {
      const response = await HttpUtils.post<Record<string, unknown>>(
        url,
        this.headers,
        payload
      );
      return createRockEnvInfo(response);
    } catch (e) {
      throw new EnvHubError(`Failed to register environment: ${e}`);
    }
  }

  /**
   * Get environment by name
   */
  async getEnv(envName: string): Promise<RockEnvInfo> {
    const url = `${this.baseUrl}/env/get`;
    const payload = { env_name: envName };

    try {
      const response = await HttpUtils.post<Record<string, unknown>>(
        url,
        this.headers,
        payload
      );
      return createRockEnvInfo(response);
    } catch (e) {
      throw new EnvHubError(`Failed to get environment ${envName}: ${e}`);
    }
  }

  /**
   * List environments
   */
  async listEnvs(options?: {
    owner?: string;
    tags?: string[];
  }): Promise<RockEnvInfo[]> {
    const url = `${this.baseUrl}/env/list`;
    const payload = {
      owner: options?.owner,
      tags: options?.tags,
    };

    try {
      const response = await HttpUtils.post<{ envs: Record<string, unknown>[] }>(
        url,
        this.headers,
        payload
      );
      const envsData = response.envs ?? [];
      return envsData.map((envData) => createRockEnvInfo(envData));
    } catch (e) {
      throw new EnvHubError(`Failed to list environments: ${e}`);
    }
  }

  /**
   * Delete environment
   */
  async deleteEnv(envName: string): Promise<boolean> {
    const url = `${this.baseUrl}/env/delete`;
    const payload = { env_name: envName };

    try {
      await HttpUtils.post(url, this.headers, payload);
      return true;
    } catch (e) {
      const errorMessage = e instanceof Error ? e.message : String(e);
      if (errorMessage.includes('404')) {
        return false;
      }
      throw new EnvHubError(`Failed to delete environment ${envName}: ${e}`);
    }
  }

  /**
   * Health check
   */
  async healthCheck(): Promise<Record<string, string>> {
    const url = `${this.baseUrl}/health`;

    try {
      const response = await HttpUtils.get<Record<string, string>>(
        url,
        this.headers
      );
      return response;
    } catch (e) {
      throw new EnvHubError(`Failed to health check: ${e}`);
    }
  }
}
