/**
 * HTTP utilities using axios
 */

import axios, { AxiosInstance, AxiosError } from 'axios';
import https from 'https';
import { PID_PREFIX, PID_SUFFIX } from '../common/constants.js';
import { objectToCamel, objectToSnake } from './case.js';

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
   * Automatically converts request body from camelCase to snake_case
   * Automatically converts response from snake_case to camelCase
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

    // Convert request body to snake_case for API
    const snakeData = objectToSnake(data);

    try {
      const response = await client.post<unknown>(url, snakeData);
      // Convert response to camelCase for SDK users
      return objectToCamel(response.data as object) as T;
    } catch (error) {
      if (error instanceof AxiosError) {
        throw new Error(`Failed to POST ${url}: ${error.message}`);
      }
      throw error;
    }
  }

  /**
   * Send GET request
   * Automatically converts response from snake_case to camelCase
   */
  static async get<T = unknown>(
    url: string,
    headers: Record<string, string>
  ): Promise<T> {
    const client = this.createClient({ headers });

    try {
      const response = await client.get<unknown>(url);
      // Convert response to camelCase for SDK users
      return objectToCamel(response.data as object) as T;
    } catch (error) {
      if (error instanceof AxiosError) {
        throw new Error(`Failed to GET ${url}: ${error.message}`);
      }
      throw error;
    }
  }

  /**
   * Convert camelCase key to snake_case
   */
  private static camelToSnakeKey(key: string): string {
    return key.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`);
  }

  /**
   * Send multipart/form-data request
   * Automatically converts form data keys to snake_case
   * Automatically converts response from snake_case to camelCase
   */
  static async postMultipart<T = unknown>(
    url: string,
    headers: Record<string, string>,
    data?: Record<string, string | number | boolean>,
    files?: Record<string, File | Buffer | [string, Buffer, string]>
  ): Promise<T> {
    const formData = new FormData();

    // Add form fields (convert keys to snake_case)
    if (data) {
      for (const [key, value] of Object.entries(data)) {
        if (value !== undefined && value !== null) {
          const snakeKey = this.camelToSnakeKey(key);
          formData.append(snakeKey, String(value));
        }
      }
    }

    // Add files (convert field names to snake_case)
    if (files) {
      for (const [fieldName, fileData] of Object.entries(files)) {
        if (fileData !== undefined && fileData !== null) {
          const snakeFieldName = this.camelToSnakeKey(fieldName);
          if (Array.isArray(fileData)) {
            // [filename, content, contentType]
            const [filename, content, contentType] = fileData;
            const blob = new Blob([content], { type: contentType });
            formData.append(snakeFieldName, blob, filename);
          } else if (fileData instanceof Buffer) {
            const blob = new Blob([fileData], { type: 'application/octet-stream' });
            formData.append(snakeFieldName, blob, 'file');
          } else if (fileData instanceof File) {
            formData.append(snakeFieldName, fileData);
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
      const response = await client.post<unknown>(url, formData);
      // Convert response to camelCase for SDK users
      return objectToCamel(response.data as object) as T;
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
