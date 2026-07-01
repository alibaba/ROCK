/**
 * File handler for reading/writing LLM requests and responses.
 *
 * Mirrors rock/sdk/model/server/file_handler.py.
 */

import { open, readFile, stat } from 'fs/promises';
import { appendFileSync, existsSync } from 'fs';
import { initLogger } from '../../logger.js';
import {
  getLogFile,
  POLLING_INTERVAL_SECONDS,
  REQUEST_START_MARKER,
  REQUEST_END_MARKER,
  RESPONSE_START_MARKER,
  RESPONSE_END_MARKER,
  SESSION_END_MARKER,
} from './config.js';
import { sleep } from '../../utils/retry.js';

const logger = initLogger('rock.model.server.file_handler');

/**
 * Handles file-based communication with the Roll process.
 */
export class FileHandler {
  private logFile: string;

  constructor(logFile?: string) {
    this.logFile = logFile ?? getLogFile();
  }

  /**
   * Write LLM request to log file.
   *
   * Format: LLM_REQUEST_START{json}LLM_REQUEST_END{meta}
   */
  writeRequest(requestData: Record<string, unknown>, index: number): void {
    const meta = { timestamp: Date.now(), index };
    const requestJson = JSON.stringify(requestData);
    const metaJson = JSON.stringify(meta);

    const line = `${REQUEST_START_MARKER}${requestJson}${REQUEST_END_MARKER}${metaJson}\n`;

    appendFileSync(this.logFile, line, 'utf-8');
    logger.info(`Wrote request with index ${index} to log file`);
  }

  /**
   * Poll log file for response matching the request index.
   *
   * Format: LLM_RESPONSE_START{json}LLM_RESPONSE_END{meta}
   *
   * @returns The response data, or null on session end.
   * @throws On timeout.
   */
  async pollForResponse(
    requestIndex: number,
    timeoutSeconds: number = 60,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown> | null> {
    const startTime = Date.now();

    // Track file position to avoid re-reading entire file
    let lastPosition = 0;

    while (true) {
      // Check timeout
      if ((Date.now() - startTime) / 1000 > timeoutSeconds) {
        throw new Error(
          `pollForResponse timed out after ${timeoutSeconds} seconds for index ${requestIndex}`,
        );
      }

      // Check abort signal
      if (signal?.aborted) {
        throw new Error(`pollForResponse aborted for index ${requestIndex}`);
      }

      try {
        // Check if file exists
        if (!existsSync(this.logFile)) {
          await sleep(POLLING_INTERVAL_SECONDS * 1000);
          continue;
        }

        // Get file size
        const fileStats = await stat(this.logFile);
        const currentSize = fileStats.size;

        if (currentSize > lastPosition) {
          // Read new content
          const content = await readFile(this.logFile, 'utf-8');
          const allLines = content.split('\n').filter((l) => l.trim());
          lastPosition = currentSize;

          // Parse each line for matching response
          for (const line of allLines) {
            if (SESSION_END_MARKER.includes(line) || line.includes(SESSION_END_MARKER)) {
              logger.info('Session ended');
              return null;
            }

            if (line.includes(RESPONSE_START_MARKER) && line.includes(RESPONSE_END_MARKER)) {
              const { responseData, meta } = this._parseResponseLine(line);
              if (responseData && meta && meta.index === requestIndex) {
                logger.info(`Found response for index ${requestIndex}`);
                return responseData;
              }
            }
          }
        }
      } catch (e) {
        logger.error(`Error polling for response: ${e}`);
      }

      await sleep(POLLING_INTERVAL_SECONDS * 1000);
    }
  }

  /**
   * Parse a response line to extract response data and meta.
   */
  private _parseResponseLine(
    line: string,
  ): { responseData: Record<string, unknown> | null; meta: Record<string, unknown> | null } {
    try {
      const startIdx = line.indexOf(RESPONSE_START_MARKER) + RESPONSE_START_MARKER.length;
      const endIdx = line.indexOf(RESPONSE_END_MARKER);

      if (startIdx === -1 || endIdx === -1) {
        return { responseData: null, meta: null };
      }

      const responseJson = line.substring(startIdx, endIdx);
      const responseData = JSON.parse(responseJson) as Record<string, unknown>;

      const metaStart = endIdx + RESPONSE_END_MARKER.length;
      const metaJson = line.substring(metaStart).trim();
      const meta = metaJson ? (JSON.parse(metaJson) as Record<string, unknown>) : {};

      return { responseData, meta };
    } catch (e) {
      logger.error(`Error parsing response line: ${e}`);
      return { responseData: null, meta: null };
    }
  }

  /**
   * Write SESSION_END marker to log file.
   */
  writeSessionEnd(): void {
    appendFileSync(this.logFile, `${SESSION_END_MARKER}\n`, 'utf-8');
    logger.info('Wrote SESSION_END to log file');
  }
}
