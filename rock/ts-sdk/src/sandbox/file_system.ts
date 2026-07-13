/**
 * FileSystem - File system operations for sandbox
 */

import { existsSync, statSync, mkdtempSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join, resolve, basename } from 'path';
import { initLogger } from '../logger.js';
import type { Observation, CommandResponse, DownloadFileResponse } from '../types/responses.js';
import type { ChownRequest, ChmodRequest, DownloadOptions, ProgressInfo } from '../types/requests.js';
import type { AbstractSandbox, Sandbox } from './client.js';
import { RunMode } from '../common/constants.js';
import { validatePath, validateUsername, validateChmodMode, shellQuote } from '../utils/shell.js';
import { ENSURE_OSSUTIL_SCRIPT } from './constants.js';
import { envVars } from '../env_vars.js';

const logger = initLogger('rock.sandbox.fs');

/**
 * Create a tar.gz archive of a directory (simplified implementation)
 * Uses shell command `tar` for reliability
 */
async function createTarGz(sourceDir: string, outputPath: string): Promise<void> {
  const { spawn } = await import('child_process');
  
  return new Promise((resolve, reject) => {
    const tar = spawn('tar', ['-czf', outputPath, '-C', sourceDir, '.']);
    
    let stderr = '';
    tar.stderr.on('data', (data) => {
      stderr += data.toString();
    });
    
    tar.on('close', (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`tar command failed with code ${code}: ${stderr}`));
      }
    });
    
    tar.on('error', (err) => {
      reject(err);
    });
  });
}

/**
 * Abstract file system interface
 */
export abstract class FileSystem {
  protected sandbox: AbstractSandbox;

  constructor(sandbox: AbstractSandbox) {
    this.sandbox = sandbox;
  }

  abstract chown(request: ChownRequest): Promise<{ success: boolean; message: string }>;
  abstract chmod(request: ChmodRequest): Promise<{ success: boolean; message: string }>;
  abstract uploadDir(
    sourceDir: string,
    targetDir: string,
    extractTimeout?: number
  ): Promise<Observation>;
}

/**
 * Linux file system implementation
 */
export class LinuxFileSystem extends FileSystem {
  constructor(sandbox: AbstractSandbox) {
    super(sandbox);
  }

  async chown(request: ChownRequest): Promise<{ success: boolean; message: string }> {
    const { paths, recursive, remoteUser } = request;

    if (!paths || paths.length === 0) {
      throw new Error('paths is empty');
    }

    // Validate username to prevent injection
    validateUsername(remoteUser);

    // Validate all paths
    for (const p of paths) {
      validatePath(p);
    }

    const command = ['chown'];
    if (recursive) {
      command.push('-R');
    }
    command.push(`${remoteUser}:${remoteUser}`, ...paths);

    logger.info(`chown command: ${command.join(' ')}`);

    const response: CommandResponse = await this.sandbox.execute({ command, timeout: 300 });
    if (response.exitCode !== 0) {
      return { success: false, message: JSON.stringify(response) };
    }
    return { success: true, message: JSON.stringify(response) };
  }

  async chmod(request: ChmodRequest): Promise<{ success: boolean; message: string }> {
    const { paths, recursive, mode } = request;

    if (!paths || paths.length === 0) {
      throw new Error('paths is empty');
    }

    // Validate mode to prevent injection
    validateChmodMode(mode);

    // Validate all paths
    for (const p of paths) {
      validatePath(p);
    }

    const command = ['chmod'];
    if (recursive) {
      command.push('-R');
    }
    command.push(mode, ...paths);

    logger.info(`chmod command: ${command.join(' ')}`);
    const response: CommandResponse = await this.sandbox.execute({ command, timeout: 300 });
    if (response.exitCode !== 0) {
      return { success: false, message: JSON.stringify(response) };
    }
    return { success: true, message: JSON.stringify(response) };
  }

  async uploadDir(
    sourceDir: string,
    targetDir: string,
    extractTimeout: number = 600
  ): Promise<Observation> {
    let localTarPath: string | null = null;
    let remoteTarPath: string | null = null;
    let session: string | null = null;

    try {
      // Validate source directory
      const src = resolve(sourceDir);
      if (!existsSync(src)) {
        return {
          output: '',
          exitCode: 1,
          failureReason: `source_dir not found: ${src}`,
          expectString: '',
        };
      }
      const stats = statSync(src);
      if (!stats.isDirectory()) {
        return {
          output: '',
          exitCode: 1,
          failureReason: `source_dir must be a directory: ${src}`,
          expectString: '',
        };
      }

      // Validate target directory for security
      try {
        validatePath(targetDir);
      } catch (e) {
        return {
          output: '',
          exitCode: 1,
          failureReason: e instanceof Error ? e.message : 'Invalid target directory',
          expectString: '',
        };
      }

      // Generate unique names using timestamp
      const ts = Date.now().toString();
      const tmpDir = mkdtempSync(join(tmpdir(), 'rock-upload-'));
      localTarPath = join(tmpDir, `rock_upload_${ts}.tar.gz`);
      remoteTarPath = `/tmp/rock_upload_${ts}.tar.gz`;
      session = `bash-${ts}`;

      logger.info(`uploadDir: ${src} -> ${targetDir}`);

      // Create bash session
      await this.sandbox.createSession({ session, startupSource: [], envEnable: false });

      // Check tar exists in sandbox
      const checkResult = await this.sandbox.arun('command -v tar >/dev/null 2>&1', {
        session,
        mode: RunMode.NORMAL,
      });
      if (checkResult.exitCode !== 0) {
        return {
          output: '',
          exitCode: 1,
          failureReason: 'sandbox has no tar command; cannot extract tarball',
          expectString: '',
        };
      }

      // Pack locally
      try {
        await createTarGz(src, localTarPath);
      } catch (e) {
        throw new Error(`tar pack failed: ${e}`);
      }

      // Upload tarball
      const uploadResponse = await this.sandbox.upload({
        sourcePath: localTarPath,
        targetPath: remoteTarPath,
      });
      if (!uploadResponse.success) {
        return {
          output: '',
          exitCode: 1,
          failureReason: `tar upload failed: ${uploadResponse.message}`,
          expectString: '',
        };
      }

      // Extract in sandbox using properly escaped paths
      const escapedTargetDir = shellQuote(targetDir);
      const escapedRemoteTar = shellQuote(remoteTarPath);
      const extractCmd = `rm -rf ${escapedTargetDir} && mkdir -p ${escapedTargetDir} && tar -xzf ${escapedRemoteTar} -C ${escapedTargetDir}`;
      const extractResult = await this.sandbox.arun(`bash -c ${shellQuote(extractCmd)}`, {
        session,
        mode: RunMode.NOHUP,
        waitTimeout: extractTimeout,
      });

      if (extractResult.exitCode !== 0) {
        return {
          output: '',
          exitCode: 1,
          failureReason: `tar extract failed: ${extractResult.output}`,
          expectString: '',
        };
      }

      // Cleanup remote tarball
      try {
        await this.sandbox.execute({ command: ['rm', '-f', remoteTarPath], timeout: 30 });
      } catch {
        // Ignore cleanup errors
      }

      return {
        output: `uploaded ${src} -> ${targetDir} via tar`,
        exitCode: 0,
        failureReason: '',
        expectString: '',
      };
    } catch (e) {
      return {
        output: '',
        exitCode: 1,
        failureReason: `upload_dir unexpected error: ${e}`,
        expectString: '',
      };
    } finally {
      // Cleanup local tarball and temp directory
      try {
        if (localTarPath) {
          const tmpDir = basename(localTarPath).replace(/\.tar\.gz$/, '');
          rmSync(join(tmpdir(), `rock-upload-${tmpDir.split('_').pop()}`), { recursive: true, force: true });
        }
      } catch {
        // Ignore cleanup errors
      }
    }
  }

  /**
   * Download file from sandbox container to local machine.
   *
   * Supports three modes:
   * - auto: Choose based on file size and OSS availability
   * - direct: Force direct download via readFile API
   * - oss: Force OSS download
   *
   * OSS availability is determined by OssClient (Layer 1 env > Layer 2 server response).
   */
  async downloadFile(
    remotePath: string,
    localPath: string,
    options?: DownloadOptions,
  ): Promise<DownloadFileResponse> {
    const downloadMode = options?.downloadMode ?? 'auto';
    const timeout = options?.timeout;
    const onProgress = options?.onProgress;

    if (!remotePath || remotePath.trim() === '') {
      return { success: false, message: 'Remote path is required' };
    }

    const checkResult = await this.sandbox.execute({ command: ['test', '-f', remotePath], timeout: 60 });
    if (checkResult.exitCode !== 0) {
      return { success: false, message: `Remote file does not exist: ${remotePath}` };
    }

    if (downloadMode === 'direct') {
      return this.downloadDirect(remotePath, localPath);
    }

    if (downloadMode === 'oss') {
      return this.downloadViaOssProxy(remotePath, localPath, timeout, onProgress);
    }

    // auto mode
    const sizeResult = await this.sandbox.execute({ command: ['stat', '-c', '%s', remotePath], timeout: 60 });
    if (sizeResult.exitCode !== 0) {
      return this.downloadDirect(remotePath, localPath);
    }

    const fileSize = parseInt(sizeResult.stdout.trim(), 10);
    const ossThreshold = 1024 * 1024;
    const ossEnabled = process.env['ROCK_OSS_ENABLE']?.toLowerCase() === 'true';

    if (ossEnabled && fileSize >= ossThreshold) {
      const ossResult = await this.downloadViaOssProxy(remotePath, localPath, timeout, onProgress);
      if (ossResult.success) return ossResult;
      logger.warn(`OSS download failed, falling back to direct: ${ossResult.message}`);
    }

    return this.downloadDirect(remotePath, localPath);
  }

  private async downloadDirect(remotePath: string, localPath: string): Promise<DownloadFileResponse> {
    try {
      const fs = await import('fs/promises');
      const path = await import('path');
      const parentDir = path.dirname(localPath);
      await fs.mkdir(parentDir, { recursive: true });

      // Use base64 encoding to safely transfer binary files
      const result = await this.sandbox.execute({
        command: ['base64', remotePath],
        timeout: 120,
      });
      if (result.exitCode === 0 && result.stdout) {
        const buffer = Buffer.from(result.stdout, 'base64');
        await fs.writeFile(localPath, buffer);
        return { success: true, message: `Successfully downloaded ${remotePath} to ${localPath}` };
      }

      // Fallback to readFile for text files or if base64 fails
      const response = await this.sandbox.readFile({ path: remotePath });
      await fs.writeFile(localPath, response.content, 'utf-8');
      return { success: true, message: `Successfully downloaded ${remotePath} to ${localPath}` };
    } catch (e) {
      return { success: false, message: `Direct download failed: ${e}` };
    }
  }

  private async downloadViaOssProxy(
    remotePath: string,
    localPath: string,
    timeout?: number,
    onProgress?: (info: ProgressInfo) => void,
  ): Promise<DownloadFileResponse> {
    const sandbox = this.sandbox as Sandbox;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const oss = (sandbox as any)._oss;
    if (!oss) return { success: false, message: 'OSS is not available' };
    if (!(await oss.ensureSetup())) return { success: false, message: 'OSS is not available' };
    if (!(await this.ensureOssutil())) return { success: false, message: 'Failed to ensure ossutil is installed and working' };
    return oss.downloadViaOss(remotePath, localPath, timeout, onProgress);
  }

  private async ensureOssutil(): Promise<boolean> {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const sandbox = this.sandbox as any;

    // Quick check: use a shell command so a missing executable returns a
    // non-zero exit code rather than failing execute() before installation can run.
    let check: CommandResponse | undefined;
    try {
      check = await this.sandbox.execute({
        command: ['sh', '-c', 'command -v ossutil >/dev/null 2>&1 && ossutil version'],
        timeout: 60,
      });
    } catch {
      // Treat probe failures as "not installed" and continue with installation.
    }
    if (check?.exitCode === 0) {
      return true;
    }

    // Match Python SDK pattern: write script to file, then execute via nohup.
    // run_in_session has an 85s server-side hard limit; nohup avoids this.
    const ts = Date.now().toString();
    const scriptPath = `/tmp/ensure_ossutil_${ts}.sh`;

    const writeResult = await sandbox.writeFile({ content: ENSURE_OSSUTIL_SCRIPT, path: scriptPath });
    if (!writeResult.success) {
      logger.warn(`Failed to write ossutil script: ${writeResult.message}`);
      return false;
    }

    const result = await sandbox.arun(`bash ${scriptPath}`, {
      mode: 'nohup',
      waitTimeout: 300,
    });

    // Cleanup script file
    try { await this.sandbox.execute({ command: ['rm', '-f', scriptPath], timeout: 10 }); } catch { /* ignore */ }

    if (result.exitCode !== 0) {
      logger.warn(`ossutil install failed: ${result.output}`);
      return false;
    }
    const verify = await this.sandbox.execute({ command: ['ossutil', 'version'], timeout: 60 });
    if (verify.exitCode !== 0) {
      logger.warn(`ossutil verify failed: ${verify.stderr}`);
      return false;
    }
    return true;
  }
}
