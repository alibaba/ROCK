/**
 * Model service for managing local LLM service
 */

import { spawn, ChildProcess } from 'child_process';
import { resolve } from 'path';
import axios from 'axios';
import { initLogger } from '../logger.js';

const logger = initLogger('rock.model.service');

/**
 * Model service configuration
 */
export interface ModelServiceConfig {
  modelServiceType?: string;
  configFile?: string;
  host?: string;
  port?: number;
  proxyBaseUrl?: string;
  retryableStatusCodes?: string;
  requestTimeout?: number;
}

/**
 * Model service for managing local LLM service
 */
export class ModelService {
  private process: ChildProcess | null = null;

  /**
   * Start sandbox service
   */
  startSandboxService(config: ModelServiceConfig = {}): ChildProcess {
    const {
      modelServiceType = 'local',
      configFile,
      host,
      port,
      proxyBaseUrl,
      retryableStatusCodes,
      requestTimeout,
    } = config;

    const cmd = ['node', 'main.js', '--type', modelServiceType];

    if (configFile) {
      cmd.push('--config-file', configFile);
    }
    if (host) {
      cmd.push('--host', host);
    }
    if (port) {
      cmd.push('--port', String(port));
    }
    if (proxyBaseUrl) {
      cmd.push('--proxy-base-url', proxyBaseUrl);
    }
    if (retryableStatusCodes) {
      cmd.push('--retryable-status-codes', retryableStatusCodes);
    }
    if (requestTimeout) {
      cmd.push('--request-timeout', String(requestTimeout));
    }

    const command = cmd[0] ?? 'node';
    this.process = spawn(command, cmd.slice(1), {
      cwd: resolve(__dirname, 'server'),
      stdio: 'inherit',
    });

    if (!this.process) {
      throw new Error('Failed to spawn model service process');
    }

    return this.process;
  }

  /**
   * Start and wait for service to be available
   */
  async start(config: ModelServiceConfig & { timeoutSeconds?: number } = {}): Promise<string> {
    const { timeoutSeconds = 30, ...serviceConfig } = config;

    const process = this.startSandboxService(serviceConfig);
    const pid = process.pid?.toString();

    if (!pid) {
      throw new Error('Failed to start model service');
    }

    const success = await this.waitServiceAvailable(
      timeoutSeconds,
      serviceConfig.host ?? '127.0.0.1',
      serviceConfig.port ?? 8080
    );

    if (!success) {
      await this.stop(pid);
      throw new Error('Model service start failed');
    }

    return pid;
  }

  /**
   * Start watching agent
   */
  async startWatchAgent(
    agentPid: number,
    host: string = '127.0.0.1',
    port: number = 8080
  ): Promise<void> {
    await axios.post(`http://${host}:${port}/v1/agent/watch`, { pid: agentPid });
  }

  /**
   * Stop service
   */
  async stop(pid: string): Promise<void> {
    const { execSync } = await import('child_process');
    try {
      execSync(`kill -9 ${pid}`);
    } catch {
      // Ignore errors
    }
  }

  /**
   * Wait for service to be available
   */
  private async waitServiceAvailable(
    timeoutSeconds: number,
    host: string,
    port: number
  ): Promise<boolean> {
    const startTime = Date.now();

    while ((Date.now() - startTime) / 1000 < timeoutSeconds) {
      try {
        await axios.get(`http://${host}:${port}/health`);
        return true;
      } catch {
        await this.sleep(1000);
      }
    }

    return false;
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}
