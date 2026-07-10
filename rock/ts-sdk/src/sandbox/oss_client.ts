/**
 * OssClient — encapsulates all OSS interactions for a Sandbox.
 *
 * Holds OSS state (bucket, token expiration, async persistence tasks) and
 * exposes upload / download / persistence operations. Composed by Sandbox.
 *
 * Design mirrors Python SDK's rock/sdk/sandbox/oss_client.py.
 */

import { createHash } from 'crypto';
import { basename } from 'path';
import { initLogger } from '../logger.js';
import { envVars } from '../env_vars.js';
import type {
  UploadResponse,
  DownloadFileResponse,
  OssCredentials,
} from '../types/responses.js';
import { OssCredentialsSchema } from '../types/responses.js';
import type { ProgressInfo } from '../types/requests.js';

const logger = initLogger('rock.sandbox.oss');

// ─── Types ────────────────────────────────────────────────────────

/**
 * Resolved OSS configuration (Layer 1 env or Layer 2 server).
 */
export interface OssClientConfig {
  endpoint: string;
  bucket: string;
  region: string;
  enabledViaEnv: boolean; // true = Layer 1 (gated by ROCK_OSS_ENABLE); false = Layer 2
  prefix: string;
}

// ─── OssClient ─────────────────────────────────────────────────────

export class OssClient {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private bucket: any = null;
  private tokenExpireTime: string | null = null;
  private clientConfig: OssClientConfig | null = null;
  private pendingPersistenceTasks: Set<Promise<void>> = new Set();

  // We need the sandbox for API calls (get_token, arun, execute).
  // Use unknown to avoid circular import; the actual type is Sandbox from client.ts
  private sandbox: unknown;

  constructor(sandbox: unknown) {
    this.sandbox = sandbox;
  }

  // ─── Static helpers ─────────────────────────────────────────────

  /**
   * Compute deterministic OSS object name.
   *
   * Uses SHA-256(sandbox_id|local_path|sandbox_path) as digest.
   * Filename derived from sandbox_path basename (preferred) or local_path basename.
   */
  static computeObjectName(
    sandboxId: string,
    localPath: string,
    sandboxPath: string,
    prefix?: string,
  ): string {
    const payload = `${sandboxId}|${localPath}|${sandboxPath}`;
    const digest = createHash('sha256').update(payload, 'utf-8').digest('hex');
    const filename = basename(sandboxPath) || basename(localPath);

    const cleanPrefix = (prefix ?? '')
      .trim()
      .replace(/^\/+|\/+$/g, '')
      .replace(/\/+/g, '/');
    if (cleanPrefix) {
      return `${cleanPrefix}/${digest}-${filename}`;
    }
    return `${digest}-${filename}`;
  }

  /**
   * Resolve OSS configuration from two layers:
   *   Layer 1: environment variables (highest priority)
   *   Layer 2: server /get_token response (fallback)
   *   Layer 3: OSS unavailable (returns null)
   *
   * @param stsResponse - The full result dict from /get_token?account=primary
   * @returns Resolved config or null if OSS is unavailable
   */
  static resolveConfig(stsResponse: Record<string, unknown>): OssClientConfig | null {
    // Layer 1: env var (highest priority)
    const envEndpoint = process.env['ROCK_OSS_BUCKET_ENDPOINT'];
    const envBucket = process.env['ROCK_OSS_BUCKET_NAME'];
    const envRegion = process.env['ROCK_OSS_BUCKET_REGION'];
    if (envEndpoint && envBucket && envRegion) {
      return {
        endpoint: envEndpoint,
        bucket: envBucket,
        region: envRegion,
        enabledViaEnv: true,
        prefix: envVars.ROCK_OSS_TRANSFER_PREFIX ?? '',
      };
    }

    // Layer 2: server response (fallback)
    const respEndpoint =
      (stsResponse['endpoint'] as string | undefined) ??
      (stsResponse['Endpoint'] as string | undefined);
    const respBucket =
      (stsResponse['bucket'] as string | undefined) ??
      (stsResponse['Bucket'] as string | undefined);
    const respRegion =
      (stsResponse['region'] as string | undefined) ??
      (stsResponse['Region'] as string | undefined);
    const respPrefix =
      (stsResponse['prefix'] as string | undefined) ??
      (stsResponse['Prefix'] as string | undefined) ??
      '';
    if (respEndpoint && respBucket && respRegion) {
      return {
        endpoint: respEndpoint,
        bucket: respBucket,
        region: respRegion,
        enabledViaEnv: false,
        prefix: respPrefix,
      };
    }

    // Layer 3: OSS unavailable
    return null;
  }

  /**
   * Normalize an OSS region string for ali-oss / ossutil.
   */
  static normalizeRegion(region: string): string {
    return region.replace(/^oss-/, '');
  }

  // ─── Public API ─────────────────────────────────────────────────

  /**
   * Whether OSS is available: bucket has been successfully initialized.
   */
  isAvailable(): boolean {
    return this.bucket !== null;
  }

  /**
   * Ensure OSS bucket is set up and token is fresh. Idempotent.
   *
   * Returns true if OSS is available, false otherwise.
   */
  async ensureSetup(): Promise<boolean> {
    if (this.bucket !== null && !this.isTokenExpired()) {
      return true;
    }
    return this.setup();
  }

  /**
   * Upload a local file to sandbox via OSS as intermediary (large file path).
   *
   * @param timeout - Optional timeout in milliseconds
   * @param onProgress - Optional progress callback
   */
  async uploadViaOss(
    sourcePath: string,
    targetPath: string,
    timeout?: number,
    onProgress?: (info: ProgressInfo) => void,
  ): Promise<UploadResponse> {
    if (!this.bucket) {
      return { success: false, message: 'OSS bucket not set up', fileName: '' };
    }

    const sandbox = this.sandbox as { sandboxId: string; arun: Function; execute: Function; url: string; buildHeaders: Function };
    const { basename: pathBasename } = await import('path');
    const fileName = pathBasename(sourcePath);

    const ossObjectName = OssClient.computeObjectName(
      sandbox.sandboxId,
      sourcePath,
      targetPath,
      this.clientConfig?.prefix,
    );

    try {
      // Upload local file to OSS
      const { stat } = await import('fs/promises');
      const stats = await stat(sourcePath);
      const fileSize = stats.size;
      const multipartThreshold = 1024 * 1024; // 1MB

      const ossTimeout = timeout ?? envVars.ROCK_OSS_TIMEOUT;

      if (fileSize >= multipartThreshold) {
        await this.bucket.multipartUpload(ossObjectName, sourcePath, {
          timeout: ossTimeout,
          partSize: multipartThreshold,
          progress: (p: number) => {
            onProgress?.({
              phase: 'upload-to-oss',
              percent: Math.round(p * 100),
            });
          },
        });
      } else {
        await this.bucket.put(ossObjectName, sourcePath, {
          timeout: ossTimeout,
          progress: (p: number) => {
            onProgress?.({
              phase: 'upload-to-oss',
              percent: Math.round(p * 100),
            });
          },
        });
      }

      // Generate signed URL for sandbox to download
      const signedUrl = this.bucket.signatureUrl(ossObjectName, { expires: 600 });

      // Notify: starting sandbox download phase
      onProgress?.({ phase: 'download-to-sandbox', percent: -1 });

      // mkdir -p target parent
      const parentDir = targetPath.replace(/\/[^/]*$/, '').replace(/^$/, '/');
      await sandbox.arun(`mkdir -p '${parentDir}'`, { mode: 'normal', timeout: 10 });

      // wget the signed URL (nohup: avoids server-side 85s session timeout)
      const downloadCmd = `wget -O '${targetPath}' '${signedUrl}'`;
      await sandbox.arun(downloadCmd, { mode: 'nohup', waitTimeout: 600 });

      // Nohup only confirms that wget exited, not that it succeeded. Verify the
      // downloaded file is complete before reporting the upload as successful.
      const sizeCheck = await sandbox.execute({ command: ['stat', '-c', '%s', targetPath], timeout: 60 });
      const { exitCode, stdout } = sizeCheck as { exitCode?: number; stdout: string };
      const remoteSizeOutput = stdout.trim();
      if (exitCode !== 0 || !/^\d+$/.test(remoteSizeOutput)) {
        return {
          success: false,
          message: `Failed to upload file ${fileName}, unable to verify sandbox file size`,
          fileName,
        };
      }

      const remoteFileSize = Number(remoteSizeOutput);
      if (remoteFileSize !== fileSize) {
        return {
          success: false,
          message: `Failed to upload file ${fileName}, expected ${fileSize} bytes, got ${remoteFileSize} bytes`,
          fileName,
        };
      }

      return {
        success: true,
        message: `Successfully uploaded file ${fileName} to ${targetPath}`,
        fileName,
      };
    } catch (e) {
      logger.warn(`upload_via_oss failed: ${e}`);
      return {
        success: false,
        message: `Failed to upload file ${fileName} to ${targetPath}: ${e}`,
        fileName,
      };
    }
  }

  /**
   * Download file from sandbox to local via OSS as intermediary.
   *
   * Note: ensureSetup must succeed before calling. Caller (LinuxFileSystem)
   * is responsible for running ensure_ossutil in the sandbox before calling here.
   *
   * @param timeout - Optional timeout in milliseconds
   * @param onProgress - Optional progress callback
   */
  async downloadViaOss(
    remotePath: string,
    localPath: string,
    timeout?: number,
    onProgress?: (info: ProgressInfo) => void,
  ): Promise<DownloadFileResponse> {
    if (!this.bucket || !this.clientConfig) {
      return { success: false, message: 'OSS is not available' };
    }

    const sandbox = this.sandbox as { sandboxId: string; arun: Function; execute: Function; getOssStsCredentials?: Function };

    // Verify source file exists in sandbox
    const check = await sandbox.execute({ command: ['test', '-f', remotePath], timeout: 60 });
    if ((check as { exitCode: number }).exitCode !== 0) {
      return {
        success: false,
        message: `Source file not found or is not a regular file in sandbox: ${remotePath}. Note: Only regular files are supported. For directories, create a tar archive first.`,
      };
    }

    // Refresh STS creds if expired
    if (this.isTokenExpired()) {
      await this.setup();
    }
    const credentials = await this.getStsCredentials();

    // Upload sandbox file to OSS via ossutil
    const ossObjectName = OssClient.computeObjectName(
      sandbox.sandboxId,
      localPath,
      remotePath,
      this.clientConfig.prefix,
    );
    const ossUrl = `oss://${this.clientConfig.bucket}/${ossObjectName}`;
    const normalizedRegion = OssClient.normalizeRegion(this.clientConfig.region ?? '');

    const ossutilInner = `ossutil cp '${remotePath}' '${ossUrl}' --access-key-id '${credentials.accessKeyId}' --access-key-secret '${credentials.accessKeySecret}' --sts-token '${credentials.securityToken}' --endpoint '${this.clientConfig.endpoint}' --region '${normalizedRegion}'`;
    const uploadCmd = `bash -c '${ossutilInner.replace(/'/g, "'\"'\"'")}'`;
    const uploadResp = await sandbox.arun(uploadCmd, { mode: 'nohup', waitTimeout: 600 });
    if ((uploadResp as { exitCode: number }).exitCode !== 0) {
      return {
        success: false,
        message: `Failed to upload file to OSS (exit_code=${(uploadResp as { exitCode: number }).exitCode}): ${(uploadResp as { output: string }).output}`,
      };
    }

    // Download from OSS to local via ali-oss
    const { dirname: pathDirname } = (await import('path'));
    const local = typeof localPath === 'string'
      ? localPath.replace(/^~/, (process.env['HOME'] ?? '/root'))
      : localPath;
    const localDir = pathDirname(local);
    const { mkdir } = await import('fs/promises');
    await mkdir(localDir, { recursive: true });

    const ossTimeout = timeout ?? envVars.ROCK_OSS_TIMEOUT;

    try {
      await this.bucket.get(ossObjectName, local, {
        timeout: ossTimeout,
        progress: (p: number) => {
          onProgress?.({
            phase: 'download-to-local',
            percent: Math.round(p * 100),
          });
        },
      });
    } catch (e) {
      return { success: false, message: `Failed to download from OSS: ${e}` };
    }

    // Cleanup OSS object
    try {
      await this.bucket.delete(ossObjectName);
    } catch {
      // Ignore cleanup errors
    }

    return { success: true, message: `Successfully downloaded ${remotePath} to ${localPath}` };
  }

  /**
   * Fire-and-forget: upload local file to OSS in the background.
   *
   * Returns the OSS object key if scheduled, null if OSS is unavailable.
   * Failures are logged as warnings; main flow is unaffected.
   */
  async scheduleAsyncPersist(localPath: string, sandboxPath: string): Promise<string | null> {
    if (!this.bucket) {
      return null;
    }

    const sandbox = this.sandbox as { sandboxId: string };

    const ossObjectName = OssClient.computeObjectName(
      sandbox.sandboxId,
      localPath,
      sandboxPath,
      this.clientConfig?.prefix,
    );

    const task = this.persistToOss(localPath, ossObjectName)
      .then(() => {
        logger.info(`OSS persisted: ${ossObjectName}`);
      })
      .catch((e: unknown) => {
        logger.warn(`OSS persistence failed for ${ossObjectName}: ${e}`);
      });

    this.pendingPersistenceTasks.add(task);
    const untrack = () => { this.pendingPersistenceTasks.delete(task); };
    task.then(untrack).catch(untrack);

    return ossObjectName;
  }

  /**
   * Wait for pending persistence tasks (with timeout).
   *
   * @param timeoutMs - Maximum time to wait in milliseconds (default 5000)
   */
  async close(timeoutMs: number = 5000): Promise<void> {
    if (this.pendingPersistenceTasks.size === 0) {
      return;
    }

    const all = Promise.allSettled([...this.pendingPersistenceTasks]);
    const timeout = new Promise<void>((resolve) => setTimeout(resolve, timeoutMs));
    await Promise.race([all, timeout]);

    if (this.pendingPersistenceTasks.size > 0) {
      logger.warn(
        `OSS persistence tasks did not finish within ${timeoutMs}ms on close ` +
        `(${this.pendingPersistenceTasks.size} pending)`,
      );
    }
  }

  // ─── Private helpers ────────────────────────────────────────────

  /**
   * Fetch STS credentials and OSS config from /get_token endpoint.
   */
  private async getStsCredentials(): Promise<OssCredentials> {
    const sandbox = this.sandbox as { url: string; buildHeaders: () => Record<string, string> };
    const HttpUtils = (await import('../utils/http.js')).HttpUtils;

    const url = `${sandbox.url}/get_token?account=primary`;
    const headers = sandbox.buildHeaders();
    const response = await HttpUtils.get<OssCredentials>(url, headers);

    if (response.status !== 'Success') {
      throw new Error(`Failed to get OSS STS token: ${JSON.stringify(response)}`);
    }

    const credentials = OssCredentialsSchema.parse(response.result);
    this.tokenExpireTime = credentials.expiration;
    return credentials;
  }

  /**
   * Whether cached token is missing, malformed, or within 5min of expiration.
   */
  private isTokenExpired(): boolean {
    if (!this.tokenExpireTime) {
      return true;
    }

    try {
      const expireTime = new Date(this.tokenExpireTime);
      const currentTime = new Date();
      const bufferMs = 5 * 60 * 1000; // 5 minutes
      return currentTime.getTime() >= (expireTime.getTime() - bufferMs);
    } catch {
      return true;
    }
  }

  /**
   * Core setup: get STS credentials -> resolve config -> initialize ali-oss bucket.
   *
   * Returns true if OSS is successfully set up, false otherwise.
   */
  private async setup(): Promise<boolean> {
    let credentials: OssCredentials;
    try {
      credentials = await this.getStsCredentials();
    } catch (e) {
      logger.warn(`Failed to get STS credentials: ${e}`);
      return false;
    }

    const config = OssClient.resolveConfig(credentials as unknown as Record<string, unknown>);
    if (!config) {
      return false;
    }

    // Layer 1 also requires ROCK_OSS_ENABLE
    if (
      config.enabledViaEnv &&
      process.env['ROCK_OSS_ENABLE']?.toLowerCase() !== 'true'
    ) {
      return false;
    }

    try {
      const OSS = (await import('ali-oss')).default;

      const ossTimeout = envVars.ROCK_OSS_TIMEOUT;

      this.bucket = new OSS({
        secure: true,
        timeout: ossTimeout,
        endpoint: config.endpoint,
        region: OssClient.normalizeRegion(config.region),
        accessKeyId: credentials.accessKeyId,
        accessKeySecret: credentials.accessKeySecret,
        stsToken: credentials.securityToken,
        bucket: config.bucket,
        refreshSTSToken: async () => {
          const newCreds = await this.getStsCredentials();
          return {
            accessKeyId: newCreds.accessKeyId,
            accessKeySecret: newCreds.accessKeySecret,
            stsToken: newCreds.securityToken,
          };
        },
        refreshSTSTokenInterval: 300000, // 5 minutes
      });

      this.clientConfig = config;
      return true;
    } catch (e) {
      logger.warn(`Failed to initialize OSS bucket: ${e}`);
      this.bucket = null;
      return false;
    }
  }

  /**
   * Execute a single persistence upload to OSS.
   */
  private async persistToOss(localPath: string, ossObjectName: string): Promise<void> {
    const { stat } = await import('fs/promises');
    const stats = await stat(localPath);
    const fileSize = stats.size;
    const multipartThreshold = 1024 * 1024; // 1MB

    if (fileSize >= multipartThreshold) {
      await this.bucket.multipartUpload(ossObjectName, localPath, {
        partSize: multipartThreshold,
        timeout: envVars.ROCK_OSS_TIMEOUT,
      });
    } else {
      await this.bucket.put(ossObjectName, localPath, {
        timeout: envVars.ROCK_OSS_TIMEOUT,
      });
    }
  }
}
