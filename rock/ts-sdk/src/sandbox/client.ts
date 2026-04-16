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
import { ENSURE_OSSUTIL_SCRIPT } from './constants.js';
import { Network } from './network.js';
import { Process } from './process.js';
import { LinuxRemoteUser } from './remote_user.js';
import { extractNohupPid } from './utils.js';
import { RunModeType, RunMode as RunModeEnum } from '../common/constants.js';
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
  DownloadFileResponseSchema,
  OssCredentialsSchema,
} from '../types/responses.js';
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
  DownloadFileResponse,
  OssCredentials,
} from '../types/responses.js';
import type {
  Command,
  CreateBashSessionRequest,
  WriteFileRequest,
  ReadFileRequest,
  UploadRequest,
  CloseSessionRequest,
  UploadMode,
  DownloadMode,
  UploadOptions,
  DownloadOptions,
  ProgressInfo,
  UploadPhase,
  DownloadPhase,
} from '../types/requests.js';
import { envVars } from '../env_vars.js';

const logger = initLogger('rock.sandbox');

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
  private cluster: string;

  // OSS-related properties
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private ossBucket: any = null;
  private ossTokenExpireTime: string = '';

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
    logger.info('Starting sandbox...');
    const url = `${this.url}/start_async`;
    const headers = this.buildHeaders();
    // Use camelCase - HTTP layer will convert to snake_case
    const data = {
      image: this.config.image,
      autoClearTime: this.config.autoClearSeconds / 60,
      autoClearTimeMinutes: this.config.autoClearSeconds / 60,
      startupTimeout: this.config.startupTimeout,
      memory: this.config.memory,
      cpus: this.config.cpus,
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
      try {
        logger.info(`Checking status... (elapsed: ${Math.round((Date.now() - startTime) / 1000)}s)`);
        // Use Promise.race to implement timeout for status check
        const statusPromise = this.getStatus();
        const timeoutPromise = new Promise<null>((_, reject) =>
          setTimeout(() => reject(new Error('Status check timeout')), checkTimeout)
        );

        const status = await Promise.race([statusPromise, timeoutPromise]);
        if (status && status.isAlive) {
          logger.info('Sandbox is alive');
          return;
        }
      } catch (e) {
        // Status check may fail temporarily during startup, continue waiting
        logger.debug(`Status check failed (will retry): ${e}`);
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

  async getStatus(): Promise<SandboxStatusResponse> {
    const url = `${this.url}/get_status?sandbox_id=${this.sandboxId}`;
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

    const sessionName = session ?? 'default';

    if (mode === 'normal') {
      // Run command directly without pre-creating session (matches Python SDK behavior)
      return this.runInSession({ command: cmd, session: sessionName, timeout });
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
    // Single-line commands can be passed directly to nohup
    // Multi-line scripts need to be wrapped with bash -c $'script'
    let effectiveCmd: string;
    if (cmd.includes('\n')) {
      // Use $'...' syntax to properly handle newlines and special characters
      effectiveCmd = `bash -c $'${cmd.replace(/'/g, "'\\''")}'`;
    } else {
      effectiveCmd = cmd;
    }

    // Start nohup process
    const nohupCommand = `nohup ${effectiveCmd} < /dev/null > ${tmpFile} 2>&1 & echo __ROCK_PID_START__$!__ROCK_PID_END__;disown`;
    const response = await this.runInSession({
      command: nohupCommand,
      session: tmpSession,
      timeout: 30,
    });

    // Check if nohup command failed (non-zero exit code and not undefined)
    if (response.exitCode !== undefined && response.exitCode !== 0) {
      return response;
    }

    // Extract PID
    const pid = extractNohupPid(response.output);
    if (!pid) {
      return {
        output: 'Failed to submit command, nohup failed to extract PID',
        exitCode: 1,
        failureReason: 'PID extraction failed',
        expectString: '',
      };
    }

    // Wait for process completion
    const success = await this.waitForProcessCompletion(pid, tmpSession, waitTimeout, waitInterval);

    // Read output
    if (ignoreOutput) {
      return {
        output: `Command executed in nohup mode. Output file: ${tmpFile}`,
        exitCode: success ? 0 : 1,
        failureReason: success ? '' : 'Process did not complete successfully',
        expectString: '',
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
      exitCode: success ? 0 : 1,
      failureReason: success ? '' : 'Process did not complete successfully',
      expectString: '',
    };
  }

  private async waitForProcessCompletion(
    pid: number,
    session: string,
    waitTimeout: number,
    waitInterval: number
  ): Promise<boolean> {
    const startTime = Date.now();
    const checkInterval = Math.max(1, waitInterval);
    const effectiveTimeout = Math.min(checkInterval * 2, waitTimeout);

    while (Date.now() - startTime < waitTimeout * 1000) {
      try {
        const result = await this.runInSession({
          command: `kill -0 ${pid}`,
          session,
          timeout: effectiveTimeout,
        });
        // If exitCode is 0, process is still running
        if (result.exitCode === 0) {
          await sleep(checkInterval * 1000);
        } else {
          // Process does not exist - completed
          return true;
        }
      } catch {
        // Process does not exist - completed
        return true;
      }
    }

    return false; // Timeout
  }

  // File operations
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
        return { success: false, message: `File not found: ${sourcePath}` };
      }

      // Check if we should use OSS upload
      const stats = await fs.stat(sourcePath);
      const fileSize = stats.size;
      const ossEnabled = envVars.ROCK_OSS_ENABLE;
      const ossThreshold = 1024 * 1024; // 1MB

      if (uploadMode === 'oss' || (uploadMode === 'auto' && ossEnabled && fileSize > ossThreshold)) {
        return this.uploadViaOss(sourcePath, targetPath, timeout, onProgress);
      }

      // Use async readFile instead of sync readFileSync
      const fileBuffer = await fs.readFile(sourcePath);
      const fileName = sourcePath.split('/').pop() ?? 'file';

      const response = await HttpUtils.postMultipart<void>(
        url,
        headers,
        { targetPath: targetPath, sandboxId: this.sandboxId ?? '' },
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

  /**
   * Get OSS STS credentials from sandbox
   */
  async getOssStsCredentials(): Promise<OssCredentials> {
    const url = `${this.url}/get_token`;
    const headers = this.buildHeaders();

    const response = await HttpUtils.get<OssCredentials>(url, headers);

    if (response.status !== 'Success') {
      throw new Error(`Failed to get OSS STS token: ${JSON.stringify(response)}`);
    }

    const credentials = OssCredentialsSchema.parse(response.result);
    this.ossTokenExpireTime = credentials.expiration;

    return credentials;
  }

  /**
   * Check if OSS token is expired (with 5-minute buffer)
   */
  isTokenExpired(): boolean {
    if (!this.ossTokenExpireTime) {
      return true;
    }

    try {
      const expireTime = new Date(this.ossTokenExpireTime);
      const currentTime = new Date();
      const bufferMs = 5 * 60 * 1000; // 5 minutes in milliseconds

      return currentTime.getTime() >= (expireTime.getTime() - bufferMs);
    } catch {
      return true;
    }
  }

  /**
   * Download file from sandbox
   * @param remotePath - File path in sandbox
   * @param localPath - Local file path
   * @param downloadMode - Download mode: 'auto' (default), 'direct', or 'oss'
   * @param timeout - Optional timeout in milliseconds for OSS mode
   */
  async downloadFile(
    remotePath: string,
    localPath: string,
    options?: DownloadOptions
  ): Promise<DownloadFileResponse> {
    // Extract options with defaults
    const downloadMode = options?.downloadMode ?? 'auto';
    const timeout = options?.timeout;
    const onProgress = options?.onProgress;

    // Validate remote path
    if (!remotePath || remotePath.trim() === '') {
      return { success: false, message: 'Remote path is required' };
    }

    // Check if remote file exists
    const checkResult = await this.execute({ command: ['test', '-f', remotePath], timeout: 60 });
    if (checkResult.exitCode !== 0) {
      return { success: false, message: `Remote file does not exist: ${remotePath}` };
    }

    // direct mode: use readFile API
    if (downloadMode === 'direct') {
      return this.downloadDirect(remotePath, localPath);
    }

    // oss mode: use OSS as intermediary
    if (downloadMode === 'oss') {
      return this.downloadViaOss(remotePath, localPath, timeout, onProgress);
    }

    // auto mode: choose based on file size and OSS availability
    // Get remote file size
    const sizeResult = await this.execute({ command: ['stat', '-c', '%s', remotePath], timeout: 60 });
    if (sizeResult.exitCode !== 0) {
      // Fall back to direct if we can't get file size
      return this.downloadDirect(remotePath, localPath);
    }

    const fileSize = parseInt(sizeResult.stdout.trim(), 10);
    const ossThreshold = 1024 * 1024; // 1MB
    const ossEnabled = envVars.ROCK_OSS_ENABLE;

    // OSS enabled AND file >= 1MB: use OSS
    if (ossEnabled && fileSize >= ossThreshold) {
      return this.downloadViaOss(remotePath, localPath, timeout, onProgress);
    }

    // Otherwise: use direct
    return this.downloadDirect(remotePath, localPath);
  }

  /**
   * Download file directly via readFile API
   */
  private async downloadDirect(remotePath: string, localPath: string): Promise<DownloadFileResponse> {
    try {
      const fs = await import('fs/promises');
      const path = await import('path');

      // Read file content from sandbox
      const response = await this.readFile({ path: remotePath });

      // Ensure parent directory exists
      const parentDir = path.dirname(localPath);
      await fs.mkdir(parentDir, { recursive: true });

      // Write to local file
      await fs.writeFile(localPath, response.content, 'utf-8');

      return { success: true, message: `Successfully downloaded ${remotePath} to ${localPath}` };
    } catch (e) {
      return { success: false, message: `Direct download failed: ${e}` };
    }
  }

  /**
   * Download file via OSS as intermediary
   */
  private async downloadViaOss(remotePath: string, localPath: string, timeout?: number, onProgress?: (info: ProgressInfo) => void): Promise<DownloadFileResponse> {
    // Check OSS is enabled
    if (!envVars.ROCK_OSS_ENABLE) {
      return {
        success: false,
        message: 'OSS download is not enabled. Please set ROCK_OSS_ENABLE=true',
      };
    }

    try {
      // Setup OSS bucket if needed
      if (this.ossBucket === null || this.isTokenExpired()) {
        await this.setupOss(timeout);
      }

      if (!this.ossBucket) {
        return { success: false, message: 'Failed to setup OSS bucket' };
      }

      // Install ossutil in sandbox
      await this.arun(ENSURE_OSSUTIL_SCRIPT, { mode: 'nohup', waitTimeout: 300 });

      // Generate unique object name
      const timestamp = Date.now();
      const fileName = remotePath.split('/').pop() ?? 'file';
      const objectName = `download-${timestamp}-${fileName}`;

      // Get STS credentials for ossutil
      const credentials = await this.getOssStsCredentials();
      const bucketName = envVars.ROCK_OSS_BUCKET_NAME ?? '';
      const region = (envVars.ROCK_OSS_BUCKET_REGION ?? '').replace(/^oss-/, ''); // Normalize: remove "oss-" prefix if present
      // Use ROCK_OSS_BUCKET_ENDPOINT if available, otherwise build from region
      // Endpoint format: "oss-cn-hangzhou.aliyuncs.com" (no protocol prefix)
      const endpoint = envVars.ROCK_OSS_BUCKET_ENDPOINT ?? `oss-${region}.aliyuncs.com`;

      // Upload from sandbox to OSS via ossutil v2
      // ossutil v2 uses command-line parameters for credentials (no separate config needed)
      // This matches the Python SDK implementation
      // Wrap with bash -c for nohup mode to ensure correct PATH (ossutil is in /usr/local/bin)
      const ossutilInnerCmd = `ossutil cp '${remotePath}' 'oss://${bucketName}/${objectName}' --access-key-id '${credentials.accessKeyId}' --access-key-secret '${credentials.accessKeySecret}' --sts-token '${credentials.securityToken}' --endpoint '${endpoint}' --region '${region}'`;
      const uploadToOssCmd = `bash -c '${ossutilInnerCmd.replace(/'/g, "'\"'\"'")}'`;
      const uploadResult = await this.arun(uploadToOssCmd, { mode: 'nohup', waitTimeout: 600 });
      if (uploadResult.exitCode !== 0) {
        return { success: false, message: `Sandbox to OSS upload failed: ${uploadResult.output}` };
      }

      // Download from OSS to local via ali-oss with timeout and progress
      const ossTimeout = timeout ?? envVars.ROCK_OSS_TIMEOUT;
      const result = await this.ossBucket.get(objectName, localPath, { 
        timeout: ossTimeout,
        progress: (p: number) => {
          onProgress?.({
            phase: 'download-to-local',
            percent: Math.round(p * 100)
          });
        }
      });

      // Cleanup OSS object
      try {
        await this.ossBucket.delete(objectName);
      } catch {
        // Ignore cleanup errors
      }

      return { success: true, message: `Successfully downloaded ${remotePath} to ${localPath}` };
    } catch (e) {
      return { success: false, message: `OSS download failed: ${e}` };
    }
  }

  /**
   * Upload file via OSS (internal method)
   * @param timeout - Optional timeout in milliseconds
   */
  private async uploadViaOss(sourcePath: string, targetPath: string, timeout?: number, onProgress?: (info: ProgressInfo) => void): Promise<UploadResponse> {
    try {
      // Setup OSS bucket if needed
      if (this.ossBucket === null || this.isTokenExpired()) {
        await this.setupOss(timeout);
      }

      if (!this.ossBucket) {
        return { success: false, message: 'Failed to setup OSS bucket' };
      }

      const timestamp = Date.now();
      const fileName = sourcePath.split('/').pop() ?? 'file';
      const objectName = `${timestamp}-${fileName}`;

      // Check file size to determine upload method
      const fs = await import('fs/promises');
      const stats = await fs.stat(sourcePath);
      const fileSize = stats.size;
      const multipartThreshold = 1024 * 1024; // 1MB

      const ossTimeout = timeout ?? envVars.ROCK_OSS_TIMEOUT;

      // Use multipartUpload for large files (>= 1MB) to avoid connection issues
      // Matches Python SDK's oss2.resumable_upload behavior
      if (fileSize >= multipartThreshold) {
        await this.ossBucket.multipartUpload(objectName, sourcePath, {
          timeout: ossTimeout,
          partSize: multipartThreshold, // 1MB per part
          progress: (p: number) => {
            onProgress?.({
              phase: 'upload-to-oss',
              percent: Math.round(p * 100)
            });
          }
        });
      } else {
        await this.ossBucket.put(objectName, sourcePath, { 
          timeout: ossTimeout,
          progress: (p: number) => {
            onProgress?.({
              phase: 'upload-to-oss',
              percent: Math.round(p * 100)
            });
          }
        });
      }

      // Generate signed URL for sandbox to download
      const signedUrl = this.ossBucket.signatureUrl(objectName, { expires: 600 });

      // Notify: starting sandbox download phase
      onProgress?.({
        phase: 'download-to-sandbox',
        percent: -1
      });
      
      // Download in sandbox using wget
      const downloadCmd = `wget -c -O '${targetPath}' '${signedUrl}'`;
      await this.arun(downloadCmd, { mode: 'nohup', waitTimeout: 600 });

      // Verify file exists in sandbox
      const checkResult = await this.execute({ command: ['test', '-f', targetPath], timeout: 60 });
      if (checkResult.exitCode !== 0) {
        return { success: false, message: 'Sandbox download phase failed' };
      }

      return { success: true, message: `Successfully uploaded file ${fileName} to ${targetPath} via OSS` };
    } catch (e) {
      return { success: false, message: `OSS upload failed: ${e}` };
    }
  }

  /**
   * Setup OSS bucket with STS credentials
   * @param timeout - Optional timeout in milliseconds (defaults to ROCK_OSS_TIMEOUT env var or 300000ms)
   */
  private async setupOss(timeout?: number): Promise<void> {
    const credentials = await this.getOssStsCredentials();

    const OSS = (await import('ali-oss')).default;

    // Priority: parameter > env var > default (300000ms = 5 minutes)
    const ossTimeout = timeout ?? envVars.ROCK_OSS_TIMEOUT;

    this.ossBucket = new OSS({
      secure: true, // Use HTTPS for OSS connections
      timeout: ossTimeout,
      region: envVars.ROCK_OSS_BUCKET_REGION ?? '',
      accessKeyId: credentials.accessKeyId,
      accessKeySecret: credentials.accessKeySecret,
      stsToken: credentials.securityToken,
      bucket: envVars.ROCK_OSS_BUCKET_NAME ?? '',
      refreshSTSToken: async () => {
        const newCreds = await this.getOssStsCredentials();
        return {
          accessKeyId: newCreds.accessKeyId,
          accessKeySecret: newCreds.accessKeySecret,
          stsToken: newCreds.securityToken,
        };
      },
      refreshSTSTokenInterval: 300000, // 5 minutes
    });
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