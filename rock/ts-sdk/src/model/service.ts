/**
 * ModelService — orchestrates the lifecycle of the model server process.
 *
 * Manages starting, stopping, and health-checking the model service
 * as a child process on the host machine.
 *
 * Mirrors rock/sdk/model/service.py.
 */

import { spawn, ChildProcess } from 'child_process';
import { resolve, dirname } from 'path';
import axios from 'axios';
import { initLogger } from '../logger.js';

const logger = initLogger('rock.model.service');

// ---------------------------------------------------------------------------
// Start options
// ---------------------------------------------------------------------------

export interface ModelServiceStartOptions {
  modelServiceType?: string;
  configFile?: string;
  host?: string;
  port?: number;
  proxyBaseUrl?: string;
  retryableStatusCodes?: string;
  requestTimeout?: number;
  recordingFile?: string;
  replayFile?: string;
}

// ---------------------------------------------------------------------------
// ModelService class
// ---------------------------------------------------------------------------

export class ModelService {
  /**
   * Spawn the model service as a subprocess.
   */
  startSandboxService(options: ModelServiceStartOptions = {}): ChildProcess {
    // Use __dirname from CommonJS compatibility or compute from module path
    const serviceDir = resolve(__dirname, '..', 'model', 'server');

    const cmdArgs: string[] = ['--type', options.modelServiceType ?? 'local'];

    if (options.configFile) cmdArgs.push('--config-file', options.configFile);
    if (options.host) cmdArgs.push('--host', options.host);
    if (options.port !== undefined) cmdArgs.push('--port', String(options.port));
    if (options.proxyBaseUrl) cmdArgs.push('--proxy-base-url', options.proxyBaseUrl);
    if (options.retryableStatusCodes) cmdArgs.push('--retryable-status-codes', options.retryableStatusCodes);
    if (options.requestTimeout !== undefined) cmdArgs.push('--request-timeout', String(options.requestTimeout));
    if (options.recordingFile) cmdArgs.push('--recording-file', options.recordingFile);
    if (options.replayFile) cmdArgs.push('--replay-file', options.replayFile);

    const mainFile = resolve(serviceDir, 'main.js');
    let proc: ChildProcess;
    try {
      proc = spawn('node', [mainFile, ...cmdArgs], { cwd: serviceDir, stdio: 'pipe' });
    } catch {
      proc = spawn('npx', ['tsx', resolve(serviceDir, 'main.ts'), ...cmdArgs], { cwd: serviceDir, stdio: 'pipe' });
    }
    return proc;
  }

  /**
   * Start the model service and wait for it to become available.
   */
  async start(options: ModelServiceStartOptions = {}): Promise<string> {
    const proc = this.startSandboxService(options);
    const pid = String(proc.pid!);
    const host = options.host ?? '127.0.0.1';
    const port = options.port ?? 8080;

    const success = await this._waitServiceAvailable(30, host, port);
    if (!success) {
      await this.stop(pid);
      throw new Error('Model service start failed');
    }

    logger.info(`Model service started with pid=${pid}`);
    return pid;
  }

  /**
   * Notify the model service to start watching an agent process.
   */
  async startWatchAgent(agentPid: number, host: string = '127.0.0.1', port: number = 8080): Promise<void> {
    await axios.post(`http://${host}:${port}/v1/agent/watch`, { pid: agentPid });
  }

  /**
   * Stop the model service by killing its process.
   */
  async stop(pid: string): Promise<void> {
    try {
      const { execSync } = await import('child_process');
      execSync(`kill -9 ${pid}`);
    } catch (e) {
      logger.warn(`Failed to kill process ${pid}: ${e}`);
    }
  }

  /**
   * Wait for the model service to become available by polling /health.
   */
  async _waitServiceAvailable(
    timeoutSeconds: number,
    host: string = '127.0.0.1',
    port: number = 8080,
  ): Promise<boolean> {
    const deadline = Date.now() + timeoutSeconds * 1000;

    while (Date.now() < deadline) {
      try {
        const resp = await axios.get(`http://${host}:${port}/health`, { timeout: 2000 });
        if (resp.status === 200) return true;
      } catch {
        // not ready yet
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    return false;
  }
}
