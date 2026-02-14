/**
 * HTTP utilities using axios
 */

import axios, { AxiosInstance, AxiosError } from 'axios';
import https from 'https';
import { PID_PREFIX, PID_SUFFIX } from '../common/constants.js';

/**
 * HTTP client configuration
 */
export interface HttpConfig {
  baseURL?: string;
  timeout?: number;
  headers?: Record<string, string>;
}

/**
 * HTTP utilities class
 */
export class HttpUtils {
  private static defaultTimeout = 300000; // 5 minutes
  private static defaultConnectTimeout = 300000;

  /**
   * Create axios instance with default config
   */
  private static createClient(config?: HttpConfig): AxiosInstance {
    return axios.create({
      timeout: config?.timeout ?? this.defaultTimeout,
      headers: {
        'Content-Type': 'application/json',
        ...config?.headers,
      },
      httpsAgent: new https.Agent({
        rejectUnauthorized: true,
      }),
    });
  }

  /**
   * Send POST request
   */
  static async post<T = unknown>(
    url: string,
    headers: Record<string, string>,
    data: Record<string, unknown>,
    readTimeout?: number
  ): Promise<T> {
    const client = this.createClient({
      timeout: readTimeout ?? this.defaultTimeout,
      headers,
    });

    try {
      const response = await client.post<T>(url, data);
      return response.data;
    } catch (error) {
      if (error instanceof AxiosError) {
        throw new Error(`Failed to POST ${url}: ${error.message}`);
      }
      throw error;
    }
  }

  /**
   * Send GET request
   */
  static async get<T = unknown>(
    url: string,
    headers: Record<string, string>
  ): Promise<T> {
    const client = this.createClient({ headers });

    try {
      const response = await client.get<T>(url);
      return response.data;
    } catch (error) {
      if (error instanceof AxiosError) {
        throw new Error(`Failed to GET ${url}: ${error.message}`);
      }
      throw error;
    }
  }

  /**
   * Send multipart/form-data request
   */
  static async postMultipart<T = unknown>(
    url: string,
    headers: Record<string, string>,
    data?: Record<string, string | number | boolean>,
    files?: Record<string, File | Buffer | [string, Buffer, string]>
  ): Promise<T> {
    const formData = new FormData();

    // Add form fields
    if (data) {
      for (const [key, value] of Object.entries(data)) {
        if (value !== undefined && value !== null) {
          formData.append(key, String(value));
        }
      }
    }

    // Add files
    if (files) {
      for (const [fieldName, fileData] of Object.entries(files)) {
        if (fileData !== undefined && fileData !== null) {
          if (Array.isArray(fileData)) {
            // [filename, content, contentType]
            const [filename, content, contentType] = fileData;
            const blob = new Blob([content], { type: contentType });
            formData.append(fieldName, blob, filename);
          } else if (fileData instanceof Buffer) {
            const blob = new Blob([fileData], { type: 'application/octet-stream' });
            formData.append(fieldName, blob, 'file');
          } else if (fileData instanceof File) {
            formData.append(fieldName, fileData);
          }
        }
      }
    }

    const client = this.createClient({
      headers: {
        ...headers,
        'Content-Type': 'multipart/form-data',
      },
    });

    try {
      const response = await client.post<T>(url, formData);
      return response.data;
    } catch (error) {
      if (error instanceof AxiosError) {
        throw new Error(`Failed to POST multipart ${url}: ${error.message}`);
      }
      throw error;
    }
  }

  /**
   * Guess MIME type from filename
   */
  static guessContentType(filename: string): string {
    const ext = filename.split('.').pop()?.toLowerCase();
    const mimeTypes: Record<string, string> = {
      txt: 'text/plain',
      html: 'text/html',
      css: 'text/css',
      js: 'application/javascript',
      json: 'application/json',
      xml: 'application/xml',
      pdf: 'application/pdf',
      zip: 'application/zip',
      tar: 'application/x-tar',
      gz: 'application/gzip',
      png: 'image/png',
      jpg: 'image/jpeg',
      jpeg: 'image/jpeg',
      gif: 'image/gif',
      svg: 'image/svg+xml',
    };

    return mimeTypes[ext ?? ''] ?? 'application/octet-stream';
  }
}

/**
 * Extract nohup PID from output
 */
export function extractNohupPid(output: string): number | null {
  const pattern = new RegExp(`${PID_PREFIX}(\\d+)${PID_SUFFIX}`);
  const match = output.match(pattern);
  if (match?.[1]) {
    return parseInt(match[1], 10);
  }
  return null;
}
