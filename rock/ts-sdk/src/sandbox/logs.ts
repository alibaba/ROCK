/**
 * Sandbox Logs - View and download sandbox log files
 * 
 * Features:
 * - List log files in /data/logs/ directory
 * - Download log files via OSS
 * - Transparent handling of sandbox alive/destroyed state
 * - Path security validation
 */

import { initLogger } from '../logger.js';
import type { ProgressInfo, DownloadPhase } from '../types/requests.js';

const logger = initLogger('rock.sandbox.logs');

/**
 * Log file information
 */
export interface LogFileInfo {
  /** File name */
  name: string;
  /** Relative path from /data/logs/ */
  path: string;
  /** File size in bytes */
  size: number;
  /** Last modified time (ISO 8601) */
  modifiedTime: string;
  /** Is directory */
  isDirectory: boolean;
}

/**
 * Options for listing logs
 */
export interface ListLogsOptions {
  /** Recursively list subdirectories (default: true) */
  recursive?: boolean;
  /** File name pattern (glob-like), e.g. "*.log" */
  pattern?: string;
}

/**
 * Options for downloading logs
 */
export interface DownloadLogOptions {
  /** Timeout in milliseconds */
  timeout?: number;
  /** Progress callback */
  onProgress?: (info: ProgressInfo) => void;
}

/**
 * Response from downloadLog
 */
export interface DownloadLogResponse {
  success: boolean;
  message: string;
}

/**
 * Host info with sandbox alive status
 */
export interface HostInfo {
  hostIp: string;
  isAlive: boolean;
}

/**
 * Validate log path for security
 * - Must be relative path
 * - Cannot contain ".."
 * - Cannot escape /data/logs/
 */
export function validateLogPath(logPath: string): void {
  // Check for absolute path
  if (logPath.startsWith('/')) {
    throw new Error('logPath must be relative path');
  }

  // Check for directory traversal
  if (logPath.includes('..')) {
    throw new Error("logPath cannot contain '..'");
  }

  // Normalize and check for escape
  const normalized = normalizePath(logPath);
  if (normalized.startsWith('..') || normalized.startsWith('/')) {
    throw new Error('Invalid logPath');
  }
}

/**
 * Normalize path (remove redundant slashes, resolve . and ..)
 */
function normalizePath(path: string): string {
  const parts = path.split('/').filter(p => p !== '' && p !== '.');
  const result: string[] = [];
  
  for (const part of parts) {
    if (part === '..') {
      if (result.length > 0 && result[result.length - 1] !== '..') {
        result.pop();
      } else {
        result.push('..');
      }
    } else {
      result.push(part);
    }
  }
  
  return result.join('/');
}

/**
 * Get log base path based on sandbox alive status
 */
export function getLogBasePath(sandboxId: string, isAlive: boolean): string {
  if (isAlive) {
    return '/data/logs';
  } else {
    return `/data/logs/${sandboxId}`;
  }
}

/**
 * Parse file list output from find or ls command
 */
export function parseFileList(output: string, basePath: string): LogFileInfo[] {
  const files: LogFileInfo[] = [];
  const lines = output.trim().split('\n').filter(line => line.trim());

  for (const line of lines) {
    // Format: path\tsize\tmodifiedTime (from find -printf '%p\t%s\t%T@\n')
    const parts = line.split('\t');
    if (parts.length >= 3) {
      const fullPath = parts[0];
      const sizeStr = parts[1];
      const modTimeStr = parts[2];
      
      if (!fullPath || !sizeStr || !modTimeStr) {
        continue;
      }
      
      const relativePath = fullPath.replace(basePath + '/', '');
      
      if (relativePath && relativePath !== basePath) {
        files.push({
          name: relativePath.split('/').pop() ?? '',
          path: relativePath,
          size: parseInt(sizeStr, 10) || 0,
          modifiedTime: new Date(parseFloat(modTimeStr) * 1000).toISOString(),
          isDirectory: false,
        });
      }
    }
  }

  return files;
}

/**
 * Build find command for listing logs
 */
export function buildListCommand(basePath: string, options?: ListLogsOptions): string {
  const recursive = options?.recursive ?? true;
  const pattern = options?.pattern;

  let cmd = `find '${basePath}'`;
  
  if (!recursive) {
    cmd += ' -maxdepth 1';
  }
  
  cmd += ' -type f';
  
  if (pattern) {
    cmd += ` -name '${pattern}'`;
  }
  
  cmd += " -printf '%p\\t%s\\t%T@\\n'";
  
  return cmd;
}

/**
 * Check if a glob pattern matches a filename
 */
export function matchPattern(filename: string, pattern: string): boolean {
  // Convert glob pattern to regex
  const regexPattern = pattern
    .replace(/\./g, '\\.')
    .replace(/\*/g, '.*')
    .replace(/\?/g, '.');
  
  return new RegExp(`^${regexPattern}$`).test(filename);
}
