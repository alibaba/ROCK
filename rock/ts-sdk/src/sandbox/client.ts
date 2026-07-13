/**
 * Sandbox client - Core sandbox management
 */

import { randomUUID } from 'crypto';
import { initLogger } from '../logger.js';
import { raiseForCode, InternalServerRockError } from '../common/exceptions.js';
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
import { OssClient } from './oss_client.js';
import { Process } from './process.js';
import { LinuxRemoteUser } from './remote_user.js';
import { extractNohupPid } from './utils.js';
import { RunModeType, RunMode as RunModeEnum, PID_PREFIX, PID_SUFFIX } from '../common/constants.js';
export type { RunModeType };
export { RunModeEnum as RunMode };
import {
  ObservationSchema,
  CommandResponseSchema,
  IsAliveResponseSchema,
  SandboxStatusResponseSchema,
  CreateSessionResponseSchema,
  WriteFileResponseSchema,
  ReadFileResponseSchema,
  UploadResponseSchema,
  CloseSessionResponseSchema,
} from '../types/responses.js';
import type {
  Observation,
  CommandResponse,
  IsAliveResponse,
  SandboxResponse,
  SandboxStatusResponse,
  CreateSessionResponse,
  WriteFileResponse,
  ReadFileResponse,
  UploadResponse,
  CloseSessionResponse,
  DownloadFileResponse,
} from '../types/responses.js';
import type {
  Command,
  CreateBashSessionRequest,
  WriteFileRequest,
  ReadFileRequest,
  UploadRequest,
  CloseSessionRequest,
  UploadMode,
  UploadOptions,
  DownloadOptions,
} from '../types/requests.js';
import { envVars } from '../env_vars.js';

const logger = initLogger('rock.sandbox');

/**
 * MIME type lookup from file extension.
 * Maps common extensions to their MIME types.
 * In Python SDK, this uses mimetypes.guess_type().
 */
const MIME_MAP: Record<string, string> = {
  '.py': 'text/x-python',
  '.json': 'application/json',
  '.txt': 'text/plain',
  '.tar.gz': 'application/gzip',
  '.tgz': 'application/gzip',
  '.gz': 'application/gzip',
  '.zip': 'application/zip',
  '.csv': 'text/csv',
  '.yaml': 'text/yaml',
  '.yml': 'text/yaml',
  '.xml': 'application/xml',
  '.html': 'text/html',
  '.css': 'text/css',
  '.js': 'application/javascript',
  '.ts': 'application/typescript',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.pdf': 'application/pdf',
  '.md': 'text/markdown',
  '.sh': 'text/x-shellscript',
  '.log': 'text/plain',
};

function getMimeType(filename: string): string {
  // Check compound extensions first (e.g., .tar.gz)
  for (const [ext, mime] of Object.entries(MIME_MAP)) {
    if (filename.endsWith(ext)) {
      return mime;
    }
  }
  return 'application/octet-stream';
}

/**
 * Abstract sandbox interface
 */
export abstract class AbstractSandbox {
  abstract isAlive(): Promise<IsAliveResponse>;
  abstract createSession(request: CreateBashSessionRequest): Promise<CreateSessionResponse>;
  abstract execute(command: Command): Promise<CommandResponse>;
  abstract readFile(request: ReadFileRequest): Promise<ReadFileResponse>;
  abstract writeFile(request: WriteFileRequest): Promise<WriteFileResponse>;
  abstract upload(request: UploadRequest): Promise<UploadResponse>;
  abstract closeSession(request: CloseSessionRequest): Promise<CloseSessionResponse>;
  abstract delete(): Promise<void>;
  abstract restart(): Promise<void>;
  abstract archive(): Promise<void>;
  abstract commit(imageTag: string, username: string, password: string): Promise<CommandResponse | undefined>;
  abstract attach(sandboxId: string): Promise<void>;
  abstract arun(
    cmd: string,
    options?: {
      session?: string;
      mode?: RunModeType;
      timeout?: number;
      waitTimeout?: number;
      waitInterval?: number;
      responseLimitedBytesInNohup?: number;
      ignoreOutput?: boolean;
      outputFile?: string;
    }
  ): Promise<Observation>;
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
  private namespace: string | null = null;
  private experimentId: string | null = null;
  private cluster: string;

  // OSS client (delegates all OSS operations)
  private _oss: OssClient;

  // Sub-components
  private _deploy: Deploy;
  private fs: LinuxFileSystem;
  private network: Network;
  private process: Process;
  private remoteUser: LinuxRemoteUser;

  // RuntimeEnv and ModelService registry
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private _runtimeEnvs: Record<string, any> = {};
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private _modelService: any = null;

  constructor(config: Partial<SandboxConfig> = {}) {
    super();
    this.config = createSandboxConfig(config);
    this.url = `${this.config.baseUrl}/apis/envs/sandbox/v1`;
    this.routeKey = this.config.routeKey ?? randomUUID().replace(/-/g, '');
    this.cluster = this.config.cluster;

    this._deploy = new Deploy(this);
    this.fs = new LinuxFileSystem(this);
    this.network = new Network(this);
    this.process = new Process(this);
    this.remoteUser = new LinuxRemoteUser(this);
    this._oss = new OssClient(this);
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

  getNamespace(): string | null {
    return this.namespace;
  }

  getExperimentId(): string | null {
    return this.experimentId;
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
    return this._deploy;
  }

  get deploy(): Deploy {
    return this._deploy;
  }

  get runtimeEnvs(): Record<string, unknown> {
    return this._runtimeEnvs;
  }

  get modelService(): unknown {
    return this._modelService;
  }

  set modelService(ms: unknown) {
    this._modelService = ms;
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

    // DEPRECATED: XRL-Authorization support via xrlAuthorization config
    if (!headers['XRL-Authorization'] && this.config.xrlAuthorization) {
      console.warn(
        'XRL-Authorization is deprecated, use extraHeaders instead'
      );
      headers['XRL-Authorization'] = 'Bearer ' + this.config.xrlAuthorization;
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
    logger.info('Starting sandbox...');
    const url = `${this.url}/start_async`;
    const headers = this.buildHeaders();
    // Use camelCase - HTTP layer will convert to snake_case
    const data = {
      image: this.config.image,
      imageOs: this.config.imageOs,
      autoClearTime: this.config.autoClearSeconds / 60,
      autoClearTimeMinutes: this.config.autoClearSeconds / 60,
      startupTimeout: this.config.startupTimeout,
      memory: this.config.memory,
      cpus: this.config.cpus,
      numGpus: this.config.numGpus,
      acceleratorType: this.config.acceleratorType,
      registryUsername: this.config.registryUsername,
      registryPassword: this.config.registryPassword,
      useKataRuntime: this.config.useKataRuntime,
      limitCpus: this.config.limitCpus,
      sandboxId: this.config.sandboxId,
      autoDeleteSeconds: this.config.autoDeleteSeconds,
      disk: this.config.disk,
    };

    logger.debug(`Calling start_async API: ${url}`);
    logger.debug(`Request data: ${JSON.stringify(data)}`);

    const response = await HttpUtils.post<{ sandboxId?: string; hostName?: string; hostIp?: string; code?: number }>(
      url,
      headers,
      data
    );

    logger.debug(`Start sandbox response: ${JSON.stringify(response)}`);

    if (response.status !== 'Success') {
      // Check for error code and throw appropriate exception
      const code = response.result?.code;
      raiseForCode(code, `Failed to start sandbox: ${JSON.stringify(response)}`);
      // If no error code, throw generic error
      throw new Error(`Failed to start sandbox: ${JSON.stringify(response)}`);
    }

    // Response is already camelCase (converted by HTTP layer)
    this.sandboxId = response.result?.sandboxId ?? null;
    this.hostName = response.result?.hostName ?? null;
    this.hostIp = response.result?.hostIp ?? null;

    logger.info(`Sandbox ID: ${this.sandboxId}`);

    // Wait for sandbox to be alive
    // First, wait a bit for the backend to process the start request
    await sleep(2000);

    const startTime = Date.now();
    const checkTimeout = 10000; // 10s timeout for each status check
    const checkInterval = 3000; // 3s between checks

    while (Date.now() - startTime < this.config.startupTimeout * 1000) {
        let timeoutId: ReturnType<typeof setTimeout> | undefined;
      try {
        logger.info(`Checking status... (elapsed: ${Math.round((Date.now() - startTime) / 1000)}s)`);
        // Use Promise.race to implement timeout for status check
        const statusPromise = this.getStatus();
        const timeoutPromise = new Promise<null>((_, reject) => {
          timeoutId = setTimeout(() => reject(new Error('Status check timeout')), checkTimeout);
        });

        const status = await Promise.race([statusPromise, timeoutPromise]);
        if (status) {
          if (status.namespace !== undefined) {
            this.namespace = status.namespace ?? null;
          }
          if (status.experimentId !== undefined) {
            this.experimentId = status.experimentId ?? null;
          }
        }
        if (status && status.isAlive) {
          logger.info('Sandbox is alive');
          return;
        }
      } catch (e) {
        // Status check may fail temporarily during startup, continue waiting
        logger.debug(`Status check failed (will retry): ${e}`);
      } finally {
        clearTimeout(timeoutId);
      }
      await sleep(checkInterval);
    }

    throw new InternalServerRockError(
      `Failed to start sandbox within ${this.config.startupTimeout}s, sandbox: ${this.toString()}`
    );
  }

  async stop(): Promise<void> {
    if (!this.sandboxId) return;

    try {
      const url = `${this.url}/stop`;
      const headers = this.buildHeaders();
      await HttpUtils.post(url, headers, { sandboxId: this.sandboxId });
    } catch (e) {
      logger.warn(`Failed to stop sandbox, IGNORE: ${e}`);
    }
  }

  /**
   * Parse error message from sandbox status dict.
   * Traverses each stage in the status dictionary and returns the first
   * "failed" or "timeout" stage message, or null if all stages are healthy.
   */
  private parseErrorMessageFromStatus(status: Record<string, unknown> | undefined): string | null {
    if (!status) return null;
    for (const [stage, details] of Object.entries(status)) {
      if (details && typeof details === 'object') {
        const d = details as Record<string, unknown>;
        if (d.status === 'failed' || d.status === 'timeout') {
          return `${stage}: ${d.message ?? 'No message provided'}`;
        }
      }
    }
    return null;
  }

  /**
   * Delete this sandbox.
   * Sends a POST /delete request with sandbox_id. Raises on failure.
   */
  async delete(): Promise<void> {
    if (!this.sandboxId) {
      throw new Error('sandbox_id is not set, cannot delete');
    }
    const url = `${this.url}/delete`;
    const headers = this.buildHeaders();
    const data = { sandboxId: this.sandboxId };
    const response = await HttpUtils.post<SandboxResponse & { code?: number }>(url, headers, data);
    logger.debug(`Delete sandbox response: ${JSON.stringify(response)}`);
    if (response.status !== 'Success') {
      const result = response.result;
      if (result) {
        raiseForCode(result.code, `Failed to delete sandbox: ${JSON.stringify(response)}`);
      }
      throw new Error(`Failed to delete sandbox: ${JSON.stringify(response)}`);
    }
  }

  /**
   * Restart a stopped sandbox using 'docker start' (reuses existing container).
   *
   * The sandbox must be in STOPPED state before calling this method.
   * After restart, polls getStatus() until the sandbox is alive or startup_timeout expires.
   */
  async restart(): Promise<void> {
    if (!this.sandboxId) {
      throw new Error('sandbox_id is not set, cannot restart');
    }
    // 1. POST /restart
    const url = `${this.url}/restart`;
    const headers = this.buildHeaders();
    const data = { sandboxId: this.sandboxId };
    const response = await HttpUtils.post<SandboxResponse & { code?: number }>(url, headers, data);
    logger.debug(`Restart sandbox response: ${JSON.stringify(response)}`);
    if (response.status !== 'Success') {
      const result = response.result;
      if (result) {
        raiseForCode(result.code, `Failed to restart sandbox: ${JSON.stringify(response)}`);
      }
      throw new Error(`Failed to restart sandbox: ${JSON.stringify(response)}`);
    }

    // 2. Poll getStatus until alive or timeout
    const startTime = Date.now();
    while (Date.now() - startTime < this.config.startupTimeout * 1000) {
      const sandboxInfo = await this.getStatus(true); // include_all_states=true for detailed status
      logger.debug(`Restart get status response: ${JSON.stringify(sandboxInfo)}`);
      if (sandboxInfo.isAlive) {
        return;
      }
      const errorMsg = this.parseErrorMessageFromStatus(sandboxInfo.status);
      if (errorMsg) {
        throw new InternalServerRockError(
          `Failed to restart sandbox because ${errorMsg}, sandbox: ${this.toString()}`
        );
      }
      await sleep(3000);
    }
    throw new InternalServerRockError(
      `Failed to restart sandbox within ${this.config.startupTimeout}s, sandbox: ${this.toString()}`
    );
  }

  /**
   * Archive a stopped sandbox (snapshot container + upload logs).
   *
   * The sandbox must be in STOPPED state. After this call the server transitions
   * it to ARCHIVING, and a background scanner will move it to ARCHIVED once
   * both the image and log uploads are confirmed.
   */
  async archive(): Promise<void> {
    if (!this.sandboxId) {
      throw new Error('sandbox_id is not set, cannot archive');
    }
    const url = `${this.url}/sandboxes/${this.sandboxId}/archive`;
    const headers = this.buildHeaders();
    const response = await HttpUtils.post<SandboxResponse & { code?: number }>(url, headers, {});
    logger.debug(`Archive sandbox response: ${JSON.stringify(response)}`);
    if (response.status !== 'Success') {
      const result = response.result;
      if (result) {
        raiseForCode(result.code, `Failed to archive sandbox: ${JSON.stringify(response)}`);
      }
      throw new Error(`Failed to archive sandbox: ${JSON.stringify(response)}`);
    }
  }

  /**
   * Commit the sandbox container as a new Docker image.
   *
   * @param imageTag - Tag for the new image (e.g., "my-image:v1")
   * @param username - Registry username for authentication
   * @param password - Registry password for authentication
   * @returns CommandResponse with stdout, stderr, and exit_code from the commit operation,
   *          or undefined if sandbox_id is not set.
   */
  async commit(imageTag: string, username: string, password: string): Promise<CommandResponse | undefined> {
    if (!this.sandboxId) {
      return;
    }
    const url = `${this.url}/commit`;
    const headers = this.buildHeaders();
    const data = {
      sandboxId: this.sandboxId,
      imageTag,
      username,
      password,
    };
    const response = await HttpUtils.post<CommandResponse & { code?: number }>(url, headers, data);
    logger.debug(`Commit sandbox response: ${JSON.stringify(response)}`);
    if (response.status !== 'Success') {
      throw new Error(`Failed to execute command: ${JSON.stringify(response)}`);
    }
    return CommandResponseSchema.parse(response.result);
  }

  /**
   * Attach to an existing sandbox by sandbox_id.
   *
   * Reconnects to a sandbox that was previously created (e.g., in another session or by
   * another process). Fetches the sandbox status to validate the sandbox_id and sync
   * configuration (hostName, hostIp, namespace, experimentId, image, cpus, memory, userId).
   *
   * @param sandboxId - The ID of the existing sandbox to attach to.
   * @throws Error if the sandbox does not exist, is unreachable, or the returned
   *               sandbox_id does not match the requested one.
   */
  async attach(sandboxId: string): Promise<void> {
    this.sandboxId = sandboxId;
    let sandboxInfo: SandboxStatusResponse;
    try {
      sandboxInfo = await this.getStatus(true);
    } catch (e) {
      this.sandboxId = null;
      throw new Error(`Failed to attach sandbox ${sandboxId}: ${e instanceof Error ? e.message : String(e)}`);
    }
    if (sandboxInfo.sandboxId !== sandboxId) {
      this.sandboxId = null;
      throw new Error(
        `sandbox_id mismatch: requested '${sandboxId}', server returned '${sandboxInfo.sandboxId}'`
      );
    }
    this.hostName = sandboxInfo.hostName ?? null;
    this.hostIp = sandboxInfo.hostIp ?? null;
    this.namespace = sandboxInfo.namespace ?? null;
    this.experimentId = sandboxInfo.experimentId ?? null;

    // Sync config with server-side state
    this.config.sandboxId = sandboxId;
    this.config.image = sandboxInfo.image ?? this.config.image;
    this.config.cpus = sandboxInfo.cpus ?? this.config.cpus;
    this.config.memory = sandboxInfo.memory ?? this.config.memory;
    this.config.userId = sandboxInfo.userId ?? this.config.userId;
    this.config.experimentId = sandboxInfo.experimentId ?? this.config.experimentId;
    this.config.namespace = sandboxInfo.namespace ?? this.config.namespace;
  }

  async isAlive(): Promise<IsAliveResponse> {
    try {
      const status = await this.getStatus();
      // Validate response with Zod schema
      return IsAliveResponseSchema.parse({
        isAlive: status.isAlive,
        message: status.hostName ?? '',
      });
    } catch (e) {
      throw new Error(`Failed to get is alive: ${e}`);
    }
  }

  async getStatus(includeAllStates: boolean = false): Promise<SandboxStatusResponse> {
    const url = `${this.url}/get_status?sandbox_id=${this.sandboxId}&include_all_states=${includeAllStates}`;
    const headers = this.buildHeaders();
    const response = await HttpUtils.get<SandboxStatusResponse & { code?: number }>(url, headers);

    if (response.status !== 'Success') {
      const errorDetail = response.error ? `, error=${response.error}` : '';
      const code = response.result?.code;
      raiseForCode(code, `Failed to get status: status=${response.status}${errorDetail}, result=${JSON.stringify(response.result)}`);
      throw new Error(`Failed to get status: status=${response.status}${errorDetail}, result=${JSON.stringify(response.result)}`);
    }

    // Validate response with Zod schema (similar to Python: SandboxStatusResponse(**result))
    const result = SandboxStatusResponseSchema.parse(response.result);

    // Extract additional info from response headers (backward compatible)
    result.cluster = response.headers['x-rock-gateway-target-cluster'] || this.config.cluster || 'N/A';
    result.requestId = response.headers['x-request-id'] || response.headers['request-id'] || 'N/A';
    result.eagleeyeTraceid = response.headers['eagleeye-traceid'] || 'N/A';

    return result;
  }

  // Command execution
  async execute(command: Command): Promise<CommandResponse> {
    const url = `${this.url}/execute`;
    const headers = this.buildHeaders();
    const data = {
      command: command.command,
      sandboxId: this.sandboxId,
      timeout: command.timeout,
      cwd: command.cwd,
      env: command.env,
    };

    const response = await HttpUtils.post<CommandResponse & { code?: number }>(
      url,
      headers,
      data
    );

    if (response.status !== 'Success') {
      const errorDetail = response.error ? `, error=${response.error}` : '';
      const code = response.result?.code;
      raiseForCode(code, `Failed to execute command: status=${response.status}${errorDetail}, result=${JSON.stringify(response.result)}`);
      throw new Error(`Failed to execute command: status=${response.status}${errorDetail}, result=${JSON.stringify(response.result)}`);
    }

    // Validate response with Zod schema (similar to Python: CommandResponse(**result))
    return CommandResponseSchema.parse(response.result);
  }

  // Session management
  async createSession(request: CreateBashSessionRequest): Promise<CreateSessionResponse> {
    const url = `${this.url}/create_session`;
    const headers = this.buildHeaders();
    const data = {
      sandboxId: this.sandboxId,
      ...request,
    };

    const response = await HttpUtils.post<CreateSessionResponse & { code?: number }>(
      url,
      headers,
      data
    );

    if (response.status !== 'Success') {
      const errorDetail = response.error ? `, error=${response.error}` : '';
      const code = response.result?.code;
      raiseForCode(code, `Failed to create session: status=${response.status}${errorDetail}, result=${JSON.stringify(response.result)}`);
      throw new Error(`Failed to create session: status=${response.status}${errorDetail}, result=${JSON.stringify(response.result)}`);
    }

    // Validate response with Zod schema (similar to Python: CreateBashSessionResponse(**result))
    return CreateSessionResponseSchema.parse(response.result);
  }

  async closeSession(request: CloseSessionRequest): Promise<CloseSessionResponse> {
    const url = `${this.url}/close_session`;
    const headers = this.buildHeaders();
    const data = {
      sandboxId: this.sandboxId,
      ...request,
    };

    const response = await HttpUtils.post<CloseSessionResponse & { code?: number }>(
      url,
      headers,
      data
    );

    if (response.status !== 'Success') {
      const errorDetail = response.error ? `, error=${response.error}` : '';
      const code = response.result?.code;
      raiseForCode(code, `Failed to close session: status=${response.status}${errorDetail}, result=${JSON.stringify(response.result)}`);
      throw new Error(`Failed to close session: status=${response.status}${errorDetail}, result=${JSON.stringify(response.result)}`);
    }

    // Validate response with Zod schema (similar to Python: CloseSessionResponse(**result))
    return CloseSessionResponseSchema.parse(response.result ?? {});
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
    } = options;

    let sessionName = session ?? null;

    if (mode === 'normal') {
      // If no session specified, create a temporary one (matches Python SDK behavior)
      if (!sessionName) {
        sessionName = `bash-${Date.now()}`;
        await this.createSession({ session: sessionName, startupSource: [], envEnable: false });
      }
      return this.runInSession({ command: cmd, session: sessionName, timeout });
    }

    if (!sessionName) {
      sessionName = 'default';
    }

    return this.arunWithNohup(cmd, options);
  }

  private async runInSession(action: { command: string; session: string; timeout?: number }): Promise<Observation> {
    const url = `${this.url}/run_in_session`;
    const headers = this.buildHeaders();
    const data = {
      actionType: 'bash',
      session: action.session,
      command: action.command,
      sandboxId: this.sandboxId,
      timeout: action.timeout,
    };

    // Convert timeout from seconds to milliseconds for axios
    const timeoutMs = action.timeout ? action.timeout * 1000 : undefined;
    const response = await HttpUtils.post<Observation & { code?: number }>(
      url,
      headers,
      data,
      timeoutMs
    );

    if (response.status !== 'Success') {
      const errorDetail = response.error ? `, error=${response.error}` : '';
      const code = response.result?.code;
      raiseForCode(code, `Failed to run in session: status=${response.status}${errorDetail}, result=${JSON.stringify(response.result)}`);
      throw new Error(`Failed to run in session: status=${response.status}${errorDetail}, result=${JSON.stringify(response.result)}`);
    }

    // Validate response with Zod schema (similar to Python: Observation(**result))
    return ObservationSchema.parse(response.result);
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

    // Only create session if not provided (matches Python SDK behavior)
    let tmpSession: string;
    if (session === undefined || session === null) {
      tmpSession = `bash-${timestamp}`;
      await this.createSession({ session: tmpSession, startupSource: [], envEnable: false });
    } else {
      tmpSession = session;
    }

    const tmpFile = outputFile ?? `/tmp/tmp_${timestamp}.out`;

    // Wrap multi-line scripts with bash -c to avoid nohup issues
    let effectiveCmd: string;
    if (cmd.includes('\n')) {
      effectiveCmd = `bash -c $'${cmd.replace(/'/g, "'\\''")}'`;
    } else {
      effectiveCmd = cmd;
    }

    // Delegate to startNohupProcess
    const { pid, errorResponse } = await this.startNohupProcess(effectiveCmd, tmpFile, tmpSession);

    if (errorResponse) {
      return errorResponse;
    }

    if (!pid) {
      const msg = 'Failed to submit command, nohup failed to extract PID';
      return { output: msg, exitCode: 1, failureReason: msg, expectString: '' };
    }

    // Wait for process completion
    const { success, message } = await this.waitForProcessCompletion(
      pid, tmpSession, waitTimeout, waitInterval
    );

    // Delegate to handleNohupOutput
    return this.handleNohupOutput(
      tmpFile, tmpSession, success, message, ignoreOutput, responseLimitedBytesInNohup ?? null
    );
  }

  /**
   * Start a nohup process and extract its PID.
   *
   * @param cmd - User command to execute in nohup (not yet wrapped with nohup)
   * @param tmpFile - Output file path for nohup stdout/stderr
   * @param session - Bash session name
   * @returns PID (if successful) and optional error Observation
   */
  async startNohupProcess(
    cmd: string,
    tmpFile: string,
    session: string
  ): Promise<{ pid: number | null; errorResponse: Observation | null }> {
    const nohupCommand = `nohup ${cmd} < /dev/null > ${tmpFile} 2>&1 & echo ${PID_PREFIX}$!${PID_SUFFIX};disown`;

    const response = await this.runInSession({
      command: nohupCommand,
      session,
      timeout: 30,
    });

    if (response.exitCode !== undefined && response.exitCode !== 0) {
      return { pid: null, errorResponse: response };
    }

    const pid = extractNohupPid(response.output);
    if (!pid) {
      return { pid: null, errorResponse: null };
    }

    return { pid, errorResponse: null };
  }

  /**
   * Handle the output of a completed nohup process.
   *
   * @param tmpFile - Path to the output file
   * @param session - Bash session name
   * @param success - Whether the process completed successfully
   * @param message - Status message from process monitoring
   * @param ignoreOutput - Whether to ignore the actual output content
   * @param responseLimitedBytesInNohup - Maximum bytes to read from output
   * @returns Observation containing the result
   */
  async handleNohupOutput(
    tmpFile: string,
    session: string,
    success: boolean,
    message: string,
    ignoreOutput: boolean,
    responseLimitedBytesInNohup: number | null
  ): Promise<Observation> {
    if (ignoreOutput) {
      // Best-effort file size detection
      let fileSize: number | null = null;
      try {
        const sizeResult = await this.runInSession({
          command: `stat -c %s ${tmpFile} 2>/dev/null || stat -f %z ${tmpFile}`,
          session,
        });
        if (sizeResult.exitCode === 0 && /^\d+$/.test(sizeResult.output.trim())) {
          fileSize = parseInt(sizeResult.output.trim(), 10);
        }
      } catch {
        // Best-effort; ignore file-size errors
      }

      const detachedMsg = this._buildNohupDetachedMessage(tmpFile, success, message, fileSize);
      if (success) {
        return { output: detachedMsg, exitCode: 0, failureReason: '', expectString: '' };
      }
      return { output: detachedMsg, exitCode: 1, failureReason: message, expectString: '' };
    }

    // Read output from file
    const readCmd = responseLimitedBytesInNohup
      ? `head -c ${responseLimitedBytesInNohup} ${tmpFile}`
      : `cat ${tmpFile}`;

    const execResult = await this.runInSession({
      command: readCmd,
      session,
    });

    if (success) {
      return { output: execResult.output, exitCode: 0, failureReason: '', expectString: '' };
    }
    return { output: execResult.output, exitCode: 1, failureReason: message, expectString: '' };
  }

  /**
   * Build a detached-mode message describing the nohup output file.
   */
  private _buildNohupDetachedMessage(
    tmpFile: string,
    success: boolean,
    message: string,
    fileSize: number | null
  ): string {
    const status = success ? 'completed successfully' : 'did not complete';
    let msg = `Command executed in nohup mode (${status}). Output file: ${tmpFile}`;
    if (message) {
      msg += `. ${message}`;
    }
    if (fileSize !== null) {
      let sizeStr: string;
      if (fileSize < 1024) {
        sizeStr = `${fileSize} bytes`;
      } else if (fileSize < 1024 * 1024) {
        sizeStr = `${(fileSize / 1024).toFixed(2)} KB`;
      } else {
        sizeStr = `${(fileSize / (1024 * 1024)).toFixed(2)} MB`;
      }
      msg += `. File size: ${sizeStr}`;
    }
    return msg;
  }

  /**
   * Wait for process completion. Public so agents can call it directly.
   *
   * @param pid - Process ID to monitor
   * @param session - Bash session name
   * @param waitTimeout - Maximum time to wait in seconds
   * @param waitInterval - Interval for checking process status in seconds
   * @returns Success status and message
   */
  async waitForProcessCompletion(
    pid: number,
    session: string,
    waitTimeout: number,
    waitInterval: number
  ): Promise<{ success: boolean; message: string }> {
    // Safety: enforce minimum and maximum bounds for wait_interval (matches Python SDK)
    waitInterval = Math.max(5, waitInterval); // Minimum interval 5 seconds
    waitInterval = Math.min(this.config.autoClearSeconds - 2, waitInterval); // wait_interval < auto_clear_seconds

    const startTime = Date.now();
    const endTime = startTime + waitTimeout * 1000;
    const checkAliveTimeout = Math.min(waitInterval * 2, waitTimeout); // Not greater than wait_timeout

    let consecutiveFailures = 0;
    const maxConsecutiveFailures = 3;

    while (Date.now() < endTime) {
      try {
        // Check if process still exists
        const result = await this.runInSession({
          command: `kill -0 ${pid}`,
          session,
          timeout: checkAliveTimeout,
        });
        // Process still exists (exitCode === 0)
        if (result.exitCode === 0) {
          // Reset failure count on successful check
          consecutiveFailures = 0;
          await sleep(waitInterval * 1000);
        } else {
          // Process does not exist - completed
          const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
          return { success: true, message: `Process completed successfully in ${elapsed}s` };
        }
      } catch (e: unknown) {
        // Match Python SDK behavior: distinguish timeout from command failure.
        // When kill -0 returns non-zero, the server returns status=Failed which
        // causes runInSession to throw — this means the process has exited.
        const isTimeout = e instanceof Error && (
          e.message.includes('timeout') || e.message.includes('ETIMEDOUT') ||
          e.message.includes('ECONNABORTED')
        );
        if (isTimeout) {
          consecutiveFailures += 1;
          if (consecutiveFailures >= maxConsecutiveFailures) {
            const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
            return { success: false, message: `Process check failed after ${elapsed}s due to consecutive timeouts` };
          }
          await sleep(waitInterval * 1000);
        } else {
          // Process does not exist or other error — consider process completed
          const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
          return { success: true, message: `Process completed successfully in ${elapsed}s` };
        }
      }
    }

    // Timeout
    const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
    return { success: false, message: `Process ${pid} did not complete within ${elapsed}s (timeout: ${waitTimeout}s)` };
  }

  // File operations

  /**
   * Download file from sandbox container to local machine.
   *
   * @deprecated Since v1.10 — use `sandbox.fs.downloadFile()` instead.
   * This wrapper is kept for backward compatibility and forwards to
   * {@link LinuxFileSystem.downloadFile}.
   */
  async downloadFile(
    remotePath: string,
    localPath: string,
    options?: DownloadOptions,
  ): Promise<DownloadFileResponse> {
    return this.fs.downloadFile(remotePath, localPath, options);
  }

  async writeFile(request: WriteFileRequest): Promise<WriteFileResponse> {
    const url = `${this.url}/write_file`;
    const headers = this.buildHeaders();
    const data = {
      content: request.content,
      path: request.path,
      sandboxId: this.sandboxId,
    };

    const response = await HttpUtils.post<void>(url, headers, data);

    if (response.status !== 'Success') {
      return { success: false, message: `Failed to write file ${request.path}` };
    }

    return { success: true, message: `Successfully write content to file ${request.path}` };
  }

  async readFile(request: ReadFileRequest): Promise<ReadFileResponse> {
    const url = `${this.url}/read_file`;
    const headers = this.buildHeaders();
    const data = {
      path: request.path,
      encoding: request.encoding,
      errors: request.errors,
      sandboxId: this.sandboxId,
    };

    const response = await HttpUtils.post<{ content: string }>(
      url,
      headers,
      data
    );

    // Validate response with Zod schema (similar to Python: ReadFileResponse(**result))
    return ReadFileResponseSchema.parse(response.result ?? {});
  }

  // Upload
  async upload(request: UploadRequest): Promise<UploadResponse> {
    return this.uploadByPath(request.sourcePath, request.targetPath);
  }

  async uploadByPath(sourcePath: string, targetPath: string, options?: UploadOptions): Promise<UploadResponse> {
    const url = `${this.url}/upload`;
    const headers = this.buildHeaders();

    // Extract options with defaults
    const uploadMode = options?.uploadMode ?? 'auto';
    const timeout = options?.timeout;
    const onProgress = options?.onProgress;

    try {
      const fs = await import('fs/promises');

      // Use async access instead of sync existsSync
      try {
        await fs.access(sourcePath);
      } catch {
        return { success: false, message: `File not found: ${sourcePath}`, fileName: '' };
      }

      // Check if we should use OSS upload
      const stats = await fs.stat(sourcePath);
      const fileSize = stats.size;
      const ossEnabled = process.env['ROCK_OSS_ENABLE']?.toLowerCase() === 'true';
      const ossThreshold = 1024 * 1024; // 1MB

      if (uploadMode === 'oss' || (uploadMode === 'auto' && ossEnabled && fileSize > ossThreshold)) {
        await this._oss.ensureSetup();
        if (this._oss.isAvailable()) {
          return this._oss.uploadViaOss(sourcePath, targetPath, timeout, onProgress);
        }
        // Explicit OSS requested but unavailable -> fail
        if (uploadMode === 'oss') {
          return {
            success: false,
            message: 'Failed to upload file, please setup oss bucket first',
            fileName: '',
          };
        }
        // Otherwise fall through to admin /upload (natural degradation for auto / default large files)
      }

      // Use async readFile instead of sync readFileSync
      const fileBuffer = await fs.readFile(sourcePath);
      const fileName = sourcePath.split('/').pop() ?? 'file';
      const contentType = getMimeType(fileName);

      const response = await HttpUtils.postMultipart<void>(
        url,
        headers,
        { targetPath: targetPath, sandboxId: this.sandboxId ?? '' },
        { file: [fileName, fileBuffer, contentType] }
      );

      if (response.status !== 'Success') {
        return { success: false, message: 'Upload failed', fileName: '' };
      }

      // Admin /upload succeeded; opportunistically persist to OSS in background.
      // Skipped silently when OSS is not configured/available.
      if (await this._oss.ensureSetup() && this._oss.isAvailable()) {
        await this._oss.scheduleAsyncPersist(sourcePath, targetPath);
      }

      return { success: true, message: `Successfully uploaded file ${fileName} to ${targetPath}`, fileName };
    } catch (e) {
      return { success: false, message: `Upload failed: ${e}`, fileName: '' };
    }
  }

  // Close
  override async close(): Promise<void> {
    // Drain pending async OSS persistence tasks (with timeout) before
    // tearing down the sandbox so in-flight uploads have a chance to finish.
    try {
      await this._oss.close();
    } catch (e) {
      logger.warn(`OssClient.close() failed, IGNORE: ${e}`);
    }
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
