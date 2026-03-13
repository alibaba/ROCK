/**
 * Model client for LLM interaction
 */

import { existsSync } from 'fs';
import { readFile, appendFile } from 'fs/promises';
import { initLogger } from '../logger.js';
import { envVars } from '../env_vars.js';
import { sleep } from '../utils/retry.js';

const logger = initLogger('rock.model.client');

/**
 * Default timeout for polling operations (in seconds)
 */
const DEFAULT_POLL_TIMEOUT = 60.0;

/**
 * Request/Response markers
 */
const REQUEST_START_MARKER = '__REQUEST_START__';
const REQUEST_END_MARKER = '__REQUEST_END__';
const RESPONSE_START_MARKER = '__RESPONSE_START__';
const RESPONSE_END_MARKER = '__RESPONSE_END__';
const SESSION_END_MARKER = '__SESSION_END__';

/**
 * Model client configuration
 */
export interface ModelClientConfig {
  logFileName?: string;
}

/**
 * Options for polling operations (popRequest, waitForFirstRequest)
 */
export interface PollOptions {
  /** Maximum time to wait in seconds. Defaults to DEFAULT_POLL_TIMEOUT */
  timeout?: number;
  /** AbortSignal for cancellation support */
  signal?: AbortSignal;
}

/**
 * Model client for LLM interaction
 */
export class ModelClient {
  private logFile: string;

  constructor(config?: ModelClientConfig) {
    this.logFile = config?.logFileName ?? envVars.ROCK_MODEL_SERVICE_DATA_DIR + '/model.log';
  }

  /**
   * Anti-call LLM - input is response, output is next request
   */
  async antiCallLlm(index: number, lastResponse?: string): Promise<string> {
    if (index < 0) {
      throw new Error('index must be greater than 0');
    }

    if (index === 0) {
      if (lastResponse !== undefined) {
        throw new Error('lastResponse must be undefined when index is 0');
      }
      await this.waitForFirstRequest();
      return this.popRequest(index + 1);
    }

    if (lastResponse === undefined) {
      throw new Error('lastResponse must not be undefined when index is greater than 0');
    }

    await this.pushResponse(index, lastResponse);
    return this.popRequest(index + 1);
  }

  /**
   * Push response to log file
   */
  async pushResponse(index: number, lastResponse: string): Promise<void> {
    const content = this.constructResponse(lastResponse, index);
    const lastResponseLine = await this.readLastResponseLine();

    if (lastResponseLine === null) {
      await this.appendResponse(content);
      return;
    }

    const { meta } = this.parseResponseLine(lastResponseLine);
    const lastResponseIndex = meta.index as number;

    if (index < lastResponseIndex) {
      throw new Error(`index ${index} must not be smaller than last_response_index ${lastResponseIndex}`);
    }

    if (index === lastResponseIndex) {
      logger.debug(`response index ${index} already exists, skip`);
      return;
    }

    await this.appendResponse(content);
  }

  /**
   * Pop request from log file
   *
   * @param index - The index of the request to pop
   * @param options - Optional configuration for timeout and cancellation
   * @returns The request JSON string or SESSION_END_MARKER
   * @throws Error if timeout expires or operation is aborted
   */
  async popRequest(index: number, options?: PollOptions): Promise<string> {
    const timeout = options?.timeout ?? DEFAULT_POLL_TIMEOUT;
    const startTime = Date.now();

    while (true) {
      // Check for abort signal
      if (options?.signal?.aborted) {
        throw new Error(`popRequest(index=${index}) aborted`);
      }

      // Check for timeout
      if ((Date.now() - startTime) / 1000 > timeout) {
        throw new Error(`popRequest timed out after ${timeout} seconds`);
      }

      try {
        const lastRequestLine = await this.readLastRequestLine();
        const { requestJson, meta } = this.parseRequestLine(lastRequestLine);

        if (requestJson === SESSION_END_MARKER) {
          return SESSION_END_MARKER;
        }

        if (meta.index === index) {
          return requestJson;
        }

        logger.debug(`Last request is not the index ${index} we want, waiting...`);
        await sleep(1000);
      } catch (e) {
        // Re-throw abort errors and parse errors immediately
        if (e instanceof Error) {
          if (e.message.includes('aborted')) {
            throw e;
          }
          // Re-throw parse errors (invalid format) immediately - don't retry
          if (e.message.includes('Invalid request line format')) {
            throw e;
          }
        }
        // For other errors (like file not found), wait and retry
        logger.debug(`Error reading request: ${e}, waiting...`);
        await sleep(1000);
      }
    }
  }

  /**
   * Wait for first request
   *
   * @param options - Optional configuration for timeout and cancellation
   * @throws Error if timeout expires or operation is aborted
   */
  async waitForFirstRequest(options?: PollOptions): Promise<void> {
    const timeout = options?.timeout ?? DEFAULT_POLL_TIMEOUT;
    const startTime = Date.now();

    while (true) {
      // Check for abort signal
      if (options?.signal?.aborted) {
        throw new Error('waitForFirstRequest aborted');
      }

      // Check for timeout
      if ((Date.now() - startTime) / 1000 > timeout) {
        throw new Error(`waitForFirstRequest timed out after ${timeout} seconds`);
      }

      if (!existsSync(this.logFile)) {
        logger.debug(`Log file ${this.logFile} not found, waiting...`);
        await sleep(1000);
        continue;
      }

      const content = await readFile(this.logFile, 'utf-8');
      const lines = content.split('\n').filter((l) => l.trim());

      if (lines.length === 0) {
        logger.debug(`Log file ${this.logFile} is empty, waiting for the first request...`);
        await sleep(1000);
        continue;
      }

      return;
    }
  }

  private parseRequestLine(lineContent: string): { requestJson: string; meta: Record<string, unknown> } {
    if (lineContent.includes(SESSION_END_MARKER)) {
      return { requestJson: SESSION_END_MARKER, meta: {} };
    }

    try {
      const parts = lineContent.split(REQUEST_END_MARKER);
      const metaJson = parts[1] ?? '';
      const requestJson = parts[0]?.split(REQUEST_START_MARKER)[1] ?? '';
      const meta = JSON.parse(metaJson);

      return { requestJson, meta };
    } catch (e) {
      logger.error(`Failed to parse request line: ${lineContent}, error: ${e}`);
      throw new Error(`Invalid request line format: ${e}`);
    }
  }

  private parseResponseLine(lineContent: string): { responseJson: string; meta: Record<string, unknown> } {
    try {
      const parts = lineContent.split(RESPONSE_END_MARKER);
      const metaJson = parts[1] ?? '';
      const responseJson = parts[0]?.split(RESPONSE_START_MARKER)[1] ?? '';
      const meta = JSON.parse(metaJson);

      return { responseJson, meta };
    } catch (e) {
      logger.error(`Failed to parse response line: ${lineContent}, error: ${e}`);
      throw new Error(`Invalid response line format: ${e}`);
    }
  }

  private async readLastRequestLine(): Promise<string> {
    const content = await readFile(this.logFile, 'utf-8');
    const lines = content.split('\n').filter((l) => l.trim());

    for (let i = lines.length - 1; i >= 0; i--) {
      const line = lines[i];
      if (line && (line.includes(REQUEST_START_MARKER) || line.includes(SESSION_END_MARKER))) {
        return line;
      }
    }

    throw new Error(`No request found in log file ${this.logFile}`);
  }

  private async readLastResponseLine(): Promise<string | null> {
    const content = await readFile(this.logFile, 'utf-8');
    const lines = content.split('\n').filter((l) => l.trim());

    for (let i = lines.length - 1; i >= 0; i--) {
      const line = lines[i];
      if (line && line.includes(RESPONSE_START_MARKER)) {
        return line;
      }
    }

    return null;
  }

  private async appendResponse(content: string): Promise<void> {
    await appendFile(this.logFile, content);
  }

  private constructResponse(lastResponse: string, index: number): string {
    const meta = {
      timestamp: Date.now(),
      index,
    };
    const metaJson = JSON.stringify(meta);
    return `${RESPONSE_START_MARKER}${lastResponse}${RESPONSE_END_MARKER}${metaJson}\n`;
  }
}
