/**
 * Local API router for the model server.
 *
 * Handles file-based communication between the Roll process and agents.
 *
 * Mirrors rock/sdk/model/server/api/local.py.
 */

import { Router, Request, Response } from 'express';
import { existsSync, mkdirSync, unlinkSync, writeFileSync } from 'fs';
import { dirname } from 'path';
import { randomUUID } from 'crypto';
import { initLogger } from '../../../logger.js';
import { getLogFile } from '../config.js';
import { FileHandler } from '../file_handler.js';

const logger = initLogger('rock.model.server.api.local');

// ---------------------------------------------------------------------------
// Globals (matching Python module-level state)
// ---------------------------------------------------------------------------

export const localRouter = Router();
let fileHandler: FileHandler;
let requestCounter = 0;

// ---------------------------------------------------------------------------
// initLocalApi
// ---------------------------------------------------------------------------

/**
 * Initialize the local API: delete old log file, create new one,
 * and instantiate the FileHandler.
 */
export async function initLocalApi(): Promise<void> {
  const logFile = getLogFile();

  if (existsSync(logFile)) {
    unlinkSync(logFile);
    logger.info(`Deleted old log file: ${logFile}`);
  }

  // Create parent directory
  const dir = dirname(logFile);
  mkdirSync(dir, { recursive: true });

  // Create new empty log file
  writeFileSync(logFile, '', 'utf-8');
  logger.info(`Created new log file: ${logFile}`);

  fileHandler = new FileHandler(logFile);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function getNextRequestIndex(): Promise<number> {
  requestCounter += 1;
  return requestCounter;
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

/**
 * GET /health
 *
 * Health check endpoint.
 */
localRouter.get('/health', (_req: Request, res: Response) => {
  res.json({ status: 'healthy' });
});

/**
 * POST /v1/agent/watch
 *
 * Start watching the agent process with the given PID.
 * When the process exits, writes SESSION_END to the log file.
 */
localRouter.post('/v1/agent/watch', async (req: Request, res: Response) => {
  try {
    const body = req.body as Record<string, unknown> | undefined;
    const agentPid = body?.pid;

    if (agentPid === undefined || agentPid === null) {
      res.status(400).json({ detail: "Missing 'pid' in request body" });
      return;
    }

    const pid = Number(agentPid);
    logger.info(`Start watching agent process with pid: ${pid}`);

    // Background task: poll every 5 seconds
    const watchInterval = setInterval(() => {
      try {
        // Check if process is alive by sending signal 0
        process.kill(pid, 0);
        logger.info(`Agent process with pid ${pid} is still running.`);
      } catch {
        // Process no longer exists
        logger.info(`Agent process with pid ${pid} has exited. Sending SESSION_END.`);
        fileHandler.writeSessionEnd();
        clearInterval(watchInterval);
      }
    }, 5000);

    res.json({ status: 'watching', pid });
  } catch (e) {
    logger.error(`Error in /v1/agent/watch: ${e}`);
    res.status(500).json({ detail: String(e) });
  }
});

/**
 * POST /v1/chat/completions
 *
 * OpenAI-compatible chat completions endpoint.
 * Handles file-based communication with the Roll process.
 *
 * Writes the incoming request to the log file, then polls for a response.
 */
localRouter.post('/v1/chat/completions', async (req: Request, res: Response) => {
  let requestIndex: number | undefined;

  try {
    const body = req.body as Record<string, unknown>;
    const requestId = `chatcmpl-${randomUUID().slice(0, 8)}`;
    requestIndex = await getNextRequestIndex();

    logger.info(`Received request ${requestId} (index: ${requestIndex})`);

    // Write request to file
    fileHandler.writeRequest(body, requestIndex);

    // Poll for response with a timeout (default 300s, matching Python's infinite polling)
    const pollTimeout = 300;
    const abortController = new AbortController();
    req.on('close', () => abortController.abort());
    const responseData = await fileHandler.pollForResponse(requestIndex, pollTimeout, abortController.signal);

    if (responseData === null) {
      res.status(500).json({ detail: 'No response received from Roll process' });
      return;
    }

    // Return response data as-is from Roll, no transformation
    res.json(responseData);
  } catch (e) {
    if (e instanceof Error && e.message.includes('aborted')) {
      logger.info(`Request ${requestIndex} was cancelled`);
      res.status(499).json({ detail: 'Request cancelled' });
      return;
    }

    logger.error(`Error processing request: ${e}`);
    res.status(500).json({ detail: `Internal server error: ${String(e)}` });
  }
});
