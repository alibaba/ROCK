/**
 * Sandbox client - Core sandbox management
 */

import axios, { AxiosInstance } from 'axios';
import { randomUUID } from 'crypto';
import { initLogger } from '../logger.js';
import { envVars } from '../env_vars.js';
import { HttpUtils } from '../utils/http.js';
import { sleep } from '../utils/retry.js';
import {
  SandboxConfig,
  SandboxGroupConfig,
  createSandboxConfig,
  createSandboxGroupConfig,
} from './config.js';
import { Deploy } from './deploy.js';
import { LinuxFileSystem } from './file_system.js';
import { Network } from './network.js';
import { Process } from './process.js';
import { LinuxRemoteUser } from './remote_user.js';
import { extractNohupPid } from './utils.js';
import { RunModeType, RunMode as RunModeEnum } from './types.js';
export type { RunModeType };
export { RunModeEnum as RunMode };
import type {
  Observation,
  CommandResponse,
  IsAliveResponse,
  SandboxStatusResponse,
  CreateSessionResponse,
  WriteFileResponse,
  ReadFileResponse,
  UploadResponse,
  CloseSessionResponse,
} from '../types/responses.js';
import type {
  Command,
  CreateBashSessionRequest,
  WriteFileRequest,
  ReadFileRequest,
  UploadRequest,
  CloseSessionRequest,
} from '../types/requests.js';

const logger = initLogger('rock.sandbox');

/**
 * Abstract sandbox interface
 */
export abstract class AbstractSandbox {
  abstract isAlive(): Promise<IsAliveResponse>;
  abstract createSession(request: CreateBashSessionRequest): Promise<CreateSessionResponse>;
  abstract execute(command: Command): Promise<CommandResponse>;
  abstract read_file(request: ReadFileRequest): Promise<ReadFileResponse>;
  abstract write_file(request: WriteFileRequest): Promise<WriteFileResponse>;
  abstract upload(request: UploadRequest): Promise<UploadResponse>;
  abstract closeSession(request: CloseSessionRequest): Promise<CloseSessionResponse>;
  abstract close(): Promise<void>;
}

/**
 * Sandbox - Main sandbox client
 */
export class Sandbox extends AbstractSandbox {
  private config: SandboxConfig;
  private url: string;
  private routeKey: string;
  private sandboxId: string | null = null;
  private hostName: string | null = null;
  private hostIp: string | null = null;
  private cluster: string;

  // Sub-components
  private deploy: Deploy;
  private fs: LinuxFileSystem;
  private network: Network;
  private process: Process;
  private remoteUser: LinuxRemoteUser;

  constructor(config: Partial<SandboxConfig> = {}) {
    super();
    this.config = createSandboxConfig(config);
    this.url = `${this.config.baseUrl}/apis/envs/sandbox/v1`;
    this.routeKey = this.config.routeKey ?? randomUUID().replace(/-/g, '');
    this.cluster = this.config.cluster;

    this.deploy = new Deploy(this);
    this.fs = new LinuxFileSystem(this);
    this.network = new Network(this);
    this.process = new Process(this);
    this.remoteUser = new LinuxRemoteUser(this);
  }

  // Getters
  getSandboxId(): string {
    if (!this.sandboxId) {
      throw new Error('Sandbox not started');
    }
    return this.sandboxId;
  }

  getHostName(): string | null {
    return this.hostName;
  }

  getHostIp(): string | null {
    return this.hostIp;
  }

  getCluster(): string {
    return this.cluster;
  }

  getUrl(): string {
    return this.url;
  }

  getFs(): LinuxFileSystem {
    return this.fs;
  }

  getNetwork(): Network {
    return this.network;
  }

  getProcess(): Process {
    return this.process;
  }

  getRemoteUser(): LinuxRemoteUser {
    return this.remoteUser;
  }

  getDeploy(): Deploy {
    return this.deploy;
  }

  getConfig(): SandboxConfig {
    return this.config;
  }

  // Build headers
  private buildHeaders(): Record<string, string> {
    const headers: Record<string, string> = {
      'ROUTE-KEY': this.routeKey,
      'X-Cluster': this.cluster,
    };

    if (this.config.extraHeaders) {
      Object.assign(headers, this.config.extraHeaders);
    }

    this.addUserDefinedTags(headers);

    return headers;
  }

  private addUserDefinedTags(headers: Record<string, string>): void {
    if (this.config.userId) {
      headers['X-User-Id'] = this.config.userId;
    }
    if (this.config.experimentId) {
      headers['X-Experiment-Id'] = this.config.experimentId;
    }
    if (this.config.namespace) {
      headers['X-Namespace'] = this.config.namespace;
    }
  }

  // Lifecycle methods
  async start(): Promise<void> {
    const url = `${this.url}/start_async`;
    const headers = this.buildHeaders();
    const data = {
      image: this.config.image,
      auto_clear_time: this.config.autoClearSeconds / 60,
      auto_clear_time_minutes: this.config.autoClearSeconds / 60,
      startup_timeout: this.config.startupTimeout,
      memory: this.config.memory,
      cpus: this.config.cpus,
    };

    logger.debug(`Calling start_async API: ${url}`);
    logger.debug(`Request data: ${JSON.stringify(data)}`);

    try {
      const response = await HttpUtils.post<{ status: string; result?: { sandbox_id?: string; host_name?: string; host_ip?: string } }>(
        url,
        headers,
        data
      );

      logger.debug(`Start sandbox response: ${JSON.stringify(response)}`);

      if (response.status !== 'Success') {
        throw new Error(`Failed to start sandbox: ${JSON.stringify(response)}`);
      }

      this.sandboxId = response.result?.sandbox_id ?? null;
      this.hostName = response.result?.host_name ?? null;
      this.hostIp = response.result?.host_ip ?? null;

      logger.debug(`Sandbox ID: ${this.sandboxId}`);

      // Wait for sandbox to be alive
      // First, wait a bit for the backend to process the start request
      await sleep(2000);

      const startTime = Date.now();
      const checkTimeout = 10000; // 10s timeout for each status check
      const checkInterval = 3000; // 3s between checks

      while (Date.now() - startTime < this.config.startupTimeout * 1000) {
        try {
          logger.debug(`Checking status... (elapsed: ${Math.round((Date.now() - startTime) / 1000)}s)`);
          // Use Promise.race to implement timeout for status check
          const statusPromise = this.getStatus();
          const timeoutPromise = new Promise<null>((_, reject) =>
            setTimeout(() => reject(new Error('Status check timeout')), checkTimeout)
          );

          const status = await Promise.race([statusPromise, timeoutPromise]);
          logger.debug(`Status result: ${JSON.stringify(status)}`);
          if (status && status.is_alive) {
            logger.debug('Sandbox is alive');
            return;
          }
        } catch (e) {
          // Status check may fail temporarily during startup, continue waiting
          logger.debug(`Status check failed (will retry): ${e}`);
        }
        await sleep(checkInterval);
      }

      throw new Error(`Failed to start sandbox within ${this.config.startupTimeout}s`);
    } catch (e) {
      throw new Error(`Failed to start sandbox: ${e}`);
    }
  }

  async stop(): Promise<void> {
    if (!this.sandboxId) return;

    try {
      const url = `${this.url}/stop`;
      const headers = this.buildHeaders();
      await HttpUtils.post(url, headers, { sandbox_id: this.sandboxId });
    } catch (e) {
      logger.warn(`Failed to stop sandbox, IGNORE: ${e}`);
    }
  }

  async isAlive(): Promise<IsAliveResponse> {
    try {
      const status = await this.getStatus();
      return {
        is_alive: status.is_alive,
        message: status.host_name ?? '',
      };
    } catch (e) {
      throw new Error(`Failed to get is alive: ${e}`);
    }
  }

  async getStatus(): Promise<SandboxStatusResponse> {
    const url = `${this.url}/get_status?sandbox_id=${this.sandboxId}`;
    const headers = this.buildHeaders();
    const response = await HttpUtils.get<{ status: string; result?: SandboxStatusResponse }>(url, headers);

    if (response.status !== 'Success') {
      throw new Error(`Failed to get status: ${JSON.stringify(response)}`);
    }

    return response.result!;
  }

  // Command execution
  async execute(command: Command): Promise<CommandResponse> {
    const url = `${this.url}/execute`;
    const headers = this.buildHeaders();
    const data = {
      command: command.command,
      sandbox_id: this.sandboxId,
      timeout: command.timeout,
      cwd: command.cwd,
      env: command.env,
    };

    try {
      const response = await HttpUtils.post<{ status: string; result?: CommandResponse }>(
        url,
        headers,
        data
      );

      if (response.status !== 'Success') {
        throw new Error(`Failed to execute command: ${JSON.stringify(response)}`);
      }

      return response.result!;
    } catch (e) {
      throw new Error(`Failed to execute command: ${e}`);
    }
  }

  // Session management
  async createSession(request: CreateBashSessionRequest): Promise<CreateSessionResponse> {
    const url = `${this.url}/create_session`;
    const headers = this.buildHeaders();
    const data = {
      sandbox_id: this.sandboxId,
      ...request,
    };

    try {
      const response = await HttpUtils.post<{ status: string; result?: CreateSessionResponse }>(
        url,
        headers,
        data
      );

      if (response.status !== 'Success') {
        throw new Error(`Failed to create session: ${JSON.stringify(response)}`);
      }

      return response.result!;
    } catch (e) {
      throw new Error(`Failed to create session: ${e}`);
    }
  }

  async closeSession(request: CloseSessionRequest): Promise<CloseSessionResponse> {
    const url = `${this.url}/close_session`;
    const headers = this.buildHeaders();
    const data = {
      sandbox_id: this.sandboxId,
      ...request,
    };

    try {
      const response = await HttpUtils.post<{ status: string; result?: CloseSessionResponse }>(
        url,
        headers,
        data
      );

      if (response.status !== 'Success') {
        throw new Error(`Failed to close session: ${JSON.stringify(response)}`);
      }

      return response.result ?? { session_type: 'bash' };
    } catch (e) {
      throw new Error(`Failed to close session: ${e}`);
    }
  }

  // Run command in session
  async arun(
    cmd: string,
    options: {
      session?: string;
      mode?: RunModeType;
      timeout?: number;
      waitTimeout?: number;
      waitInterval?: number;
      responseLimitedBytesInNohup?: number;
      ignoreOutput?: boolean;
      outputFile?: string;
    } = {}
  ): Promise<Observation> {
    const {
      session,
      mode = 'normal',
      timeout = 300,
      waitTimeout = 300,
      waitInterval = 10,
    } = options;

    const sessionName = session ?? 'default';

    if (mode === 'normal') {
      // Ensure session exists before running command (ignore if already exists)
      try {
        await this.createSession({ session: sessionName, startupSource: [], envEnable: false });
      } catch (e) {
        if (String(e).includes('already exists')) {
          // Session already exists, reuse it
        } else {
          throw e;
        }
      }
      return this.runInSession({ command: cmd, session: sessionName, timeout });
    }

    return this.arunWithNohup(cmd, options);
  }

  private async runInSession(action: { command: string; session: string; timeout?: number }): Promise<Observation> {
    const url = `${this.url}/run_in_session`;
    const headers = this.buildHeaders();
    const data = {
      action_type: 'bash',
      session: action.session,
      command: action.command,
      sandbox_id: this.sandboxId,
      timeout: action.timeout,
    };

    try {
      // Convert timeout from seconds to milliseconds for axios
      const timeoutMs = action.timeout ? action.timeout * 1000 : undefined;
      const response = await HttpUtils.post<{ status: string; result?: Record<string, unknown> }>(
        url,
        headers,
        data,
        timeoutMs
      );

      if (response.status !== 'Success') {
        throw new Error(`Failed to execute command: ${JSON.stringify(response)}`);
      }

      // Convert snake_case to camelCase
      const raw = response.result!;
      return {
        output: raw.output as string,
        exit_code: raw.exit_code as number | undefined,
        failure_reason: raw.failure_reason as string | undefined,
        expect_string: raw.expect_string as string | undefined,
      };
    } catch (e) {
      throw new Error(`Failed to run in session: ${e}`);
    }
  }

  private async arunWithNohup(
    cmd: string,
    options: {
      session?: string;
      waitTimeout?: number;
      waitInterval?: number;
      responseLimitedBytesInNohup?: number;
      ignoreOutput?: boolean;
      outputFile?: string;
    }
  ): Promise<Observation> {
    const {
      session,
      waitTimeout = 300,
      waitInterval = 10,
      responseLimitedBytesInNohup,
      ignoreOutput = false,
      outputFile,
    } = options;

    const timestamp = Date.now();
    const tmpSession = session ?? `bash-${timestamp}`;

    if (!session) {
      await this.createSession({ session: tmpSession, startupSource: [], envEnable: false });
    }

    const tmpFile = outputFile ?? `/tmp/tmp_${timestamp}.out`;

    // Start nohup process
    const nohupCommand = `nohup ${cmd} < /dev/null > ${tmpFile} 2>&1 & echo __ROCK_PID_START__$!__ROCK_PID_END__;disown`;
    const response = await this.runInSession({
      command: nohupCommand,
      session: tmpSession,
      timeout: 30,
    });

    if (response.exit_code !== 0) {
      return response;
    }

    // Extract PID
    const pid = extractNohupPid(response.output);
    if (!pid) {
      return {
        output: 'Failed to submit command, nohup failed to extract PID',
        exit_code: 1,
        failure_reason: 'PID extraction failed',
        expect_string: '',
      };
    }

    // Wait for process completion
    const success = await this.waitForProcessCompletion(pid, tmpSession, waitTimeout, waitInterval);

    // Read output
    if (ignoreOutput) {
      return {
        output: `Command executed in nohup mode. Output file: ${tmpFile}`,
        exit_code: success ? 0 : 1,
        failure_reason: success ? '' : 'Process did not complete successfully',
        expect_string: '',
      };
    }

    const readCmd = responseLimitedBytesInNohup
      ? `head -c ${responseLimitedBytesInNohup} ${tmpFile}`
      : `cat ${tmpFile}`;

    const outputResult = await this.runInSession({
      command: readCmd,
      session: tmpSession,
    });

    return {
      output: outputResult.output,
      exit_code: success ? 0 : 1,
      failure_reason: success ? '' : 'Process did not complete successfully',
      expect_string: '',
    };
  }

  private async waitForProcessCompletion(
    pid: number,
    session: string,
    waitTimeout: number,
    waitInterval: number
  ): Promise<boolean> {
    const startTime = Date.now();
    const checkInterval = Math.max(5, waitInterval);
    const effectiveInterval = Math.min(checkInterval * 2, waitTimeout);

    while (Date.now() - startTime < waitTimeout * 1000) {
      try {
        await this.runInSession({
          command: `kill -0 ${pid}`,
          session,
          timeout: effectiveInterval,
        });
        await sleep(checkInterval * 1000);
      } catch {
        // Process does not exist - completed
        return true;
      }
    }

    return false; // Timeout
  }

  // File operations
  async write_file(request: WriteFileRequest): Promise<WriteFileResponse> {
    const url = `${this.url}/write_file`;
    const headers = this.buildHeaders();
    const data = {
      content: request.content,
      path: request.path,
      sandbox_id: this.sandboxId,
    };

    const response = await HttpUtils.post<{ status: string }>(url, headers, data);

    if (response.status !== 'Success') {
      return { success: false, message: `Failed to write file ${request.path}` };
    }

    return { success: true, message: `Successfully write content to file ${request.path}` };
  }

  async read_file(request: ReadFileRequest): Promise<ReadFileResponse> {
    const url = `${this.url}/read_file`;
    const headers = this.buildHeaders();
    const data = {
      path: request.path,
      encoding: request.encoding,
      errors: request.errors,
      sandbox_id: this.sandboxId,
    };

    const response = await HttpUtils.post<{ status: string; result?: { content: string } }>(
      url,
      headers,
      data
    );

    return { content: response.result?.content ?? '' };
  }

  // Upload
  async upload(request: UploadRequest): Promise<UploadResponse> {
    return this.uploadByPath(request.sourcePath, request.targetPath);
  }

  async uploadByPath(sourcePath: string, targetPath: string): Promise<UploadResponse> {
    const url = `${this.url}/upload`;
    const headers = this.buildHeaders();

    try {
      const fs = await import('fs');
      if (!fs.existsSync(sourcePath)) {
        return { success: false, message: `File not found: ${sourcePath}` };
      }

      const fileBuffer = fs.readFileSync(sourcePath);
      const fileName = sourcePath.split('/').pop() ?? 'file';

      const response = await HttpUtils.postMultipart<{ status: string }>(
        url,
        headers,
        { target_path: targetPath, sandbox_id: this.sandboxId ?? '' },
        { file: [fileName, fileBuffer, 'application/octet-stream'] }
      );

      if (response.status !== 'Success') {
        return { success: false, message: 'Upload failed' };
      }

      return { success: true, message: `Successfully uploaded file ${fileName} to ${targetPath}` };
    } catch (e) {
      return { success: false, message: `Upload failed: ${e}` };
    }
  }

  // Close
  override async close(): Promise<void> {
    await this.stop();
  }

  override toString(): string {
    return `Sandbox(sandboxId=${this.sandboxId}, hostName=${this.hostName}, image=${this.config.image}, cluster=${this.cluster})`;
  }
}

/**
 * SandboxGroup - Group of sandboxes with concurrent operations
 */
export class SandboxGroup {
  private config: SandboxGroupConfig;
  private sandboxList: Sandbox[];

  constructor(config: Partial<SandboxGroupConfig> = {}) {
    this.config = createSandboxGroupConfig(config);
    this.sandboxList = Array.from(
      { length: this.config.size },
      () => new Sandbox(this.config)
    );
  }

  getSandboxList(): Sandbox[] {
    return this.sandboxList;
  }

  async start(): Promise<void> {
    const concurrency = this.config.startConcurrency;
    const retryTimes = this.config.startRetryTimes;

    const startSandbox = async (index: number, sandbox: Sandbox): Promise<void> => {
      logger.info(`Starting sandbox ${index} with ${sandbox.getConfig().image}...`);

      for (let attempt = 0; attempt < retryTimes; attempt++) {
        try {
          await sandbox.start();
          return;
        } catch (e) {
          if (attempt === retryTimes - 1) {
            logger.error(`Failed to start sandbox after ${retryTimes} attempts: ${e}`);
            throw e;
          }
          logger.warn(`Failed to start sandbox (attempt ${attempt + 1}/${retryTimes}): ${e}, retrying...`);
          await sleep(1000);
        }
      }
    };

    // Start with concurrency limit
    const batches: Promise<void>[] = [];
    for (let i = 0; i < this.sandboxList.length; i += concurrency) {
      const batch = this.sandboxList.slice(i, i + concurrency);
      const promises = batch.map((sandbox, idx) => startSandbox(i + idx, sandbox));
      await Promise.all(promises);
    }

    logger.info(`Successfully started ${this.sandboxList.length} sandboxes`);
  }

  async stop(): Promise<void> {
    const promises = this.sandboxList.map((sandbox) => sandbox.stop());
    await Promise.allSettled(promises);
    logger.info(`Stopped ${this.sandboxList.length} sandboxes`);
  }
}
