/**
 * OpenAI-compatible chat/completions proxy with trajectory record/replay.
 *
 * Two backends share the /v1/chat/completions route:
 *
 * 1. ForwardBackend (default) — body bytes are POSTed verbatim to the
 *    configured upstream via axios. The upstream response is forwarded
 *    byte-for-byte back to the client (raw JSON for non-stream, raw SSE bytes
 *    for stream).
 *
 * 2. ReplayBackend (replay_file set) — the request is served directly from
 *    the next record in the SequentialCursor without any upstream call.
 *
 * Mirrors rock/sdk/model/server/api/proxy.py.
 */

import { Router, Request, Response } from 'express';
import axios, { AxiosInstance } from 'axios';
import { initLogger } from '../../../logger.js';
import { ModelServiceConfig } from '../config.js';
import { parseSseDataChunks, completionToChunkDict, encodeSseEvent, SSE_DONE } from '../sse.js';
import { SequentialCursor, TrajectoryExhausted, TrajectoryRecorder } from '../traj.js';

const logger = initLogger('rock.model.server.api.proxy');

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

export const proxyRouter = Router();

// ---------------------------------------------------------------------------
// Header filtering
// ---------------------------------------------------------------------------

/** Headers we never forward upstream (hop-by-hop / rebuilt by axios). */
const HEADERS_NOT_TO_FORWARD = new Set([
  'host',
  'content-length',
  'transfer-encoding',
  'connection',
]);

/**
 * Drop headers that are scoped to the client-proxy hop or rebuilt by axios.
 * Authorization is forwarded verbatim.
 */
export function filterHeaders(
  headers: Record<string, string | string[] | undefined>,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(headers)) {
    if (HEADERS_NOT_TO_FORWARD.has(key.toLowerCase())) {
      continue;
    }
    if (typeof value === 'string') {
      out[key] = value;
    } else if (Array.isArray(value)) {
      out[key] = value.join(', ');
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Retry helpers
// ---------------------------------------------------------------------------

const RETRY_MAX_ATTEMPTS = 6;
const RETRY_DELAY_SECONDS = 2.0;
const RETRY_BACKOFF = 2.0;

function jitteredDelay(delay: number): number {
  return Math.random() * delay * 2;
}

function sleepMs(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

interface RetryResponse {
  status: number;
  headers: Record<string, string>;
  data: ReadableStream<Uint8Array> | Buffer;
}

/**
 * POST with retry on connection errors and whitelisted status codes.
 */
async function sendWithRetry(
  client: AxiosInstance,
  url: string,
  bodyBytes: Buffer,
  headers: Record<string, string>,
  retryableCodes: number[],
): Promise<{ status: number; headers: Record<string, string>; data: Buffer }> {
  let lastExc: Error | null = null;
  let delay = RETRY_DELAY_SECONDS;

  for (let attempt = 1; attempt <= RETRY_MAX_ATTEMPTS; attempt++) {
    try {
      const resp = await client.post(url, bodyBytes, {
        headers,
        responseType: 'arraybuffer',
        validateStatus: () => true, // Don't throw on any status
        timeout: 120_000,
      });

      const statusCode = resp.status;

      if (retryableCodes.includes(statusCode) && attempt < RETRY_MAX_ATTEMPTS) {
        logger.warning(
          `upstream status ${statusCode}, retry ${attempt}/${RETRY_MAX_ATTEMPTS}`,
        );
        await sleepMs(jitteredDelay(delay * 1000));
        delay *= RETRY_BACKOFF;
        continue;
      }

      // Convert headers to plain object
      const respHeaders: Record<string, string> = {};
      for (const [k, v] of Object.entries(resp.headers)) {
        if (typeof v === 'string') {
          respHeaders[k] = v;
        }
      }

      return {
        status: statusCode,
        headers: respHeaders,
        data: Buffer.from(resp.data),
      };
    } catch (e) {
      lastExc = e instanceof Error ? e : new Error(String(e));

      if (attempt >= RETRY_MAX_ATTEMPTS) {
        throw lastExc;
      }

      logger.warning(
        `connect failed (attempt ${attempt}/${RETRY_MAX_ATTEMPTS}): ${lastExc.message}`,
      );
      await sleepMs(jitteredDelay(delay * 1000));
      delay *= RETRY_BACKOFF;
    }
  }

  throw lastExc ?? new Error('unreachable');
}

// ---------------------------------------------------------------------------
// ReplayBackend
// ---------------------------------------------------------------------------

export class ReplayBackend {
  private _cursor: SequentialCursor;

  constructor(cursor: SequentialCursor) {
    this._cursor = cursor;
  }

  async serve(
    modelName: string,
    isStream: boolean,
    _bodyBytes: Buffer,
    _fwdHeaders: Record<string, string>,
    _requestDict: Record<string, unknown>,
    res: Response,
  ): Promise<void> {
    let record: Record<string, unknown>;
    try {
      record = await this._cursor.next(modelName);
    } catch (e) {
      if (e instanceof TrajectoryExhausted) {
        res.status(404).json({ detail: e.message });
        return;
      }
      throw e;
    }

    const responseDict = record.response;
    if (typeof responseDict !== 'object' || responseDict === null) {
      res.status(500).json({
        detail: `replay record at step ${this._cursor.position - 1} has no usable response dict`,
      });
      return;
    }

    logger.info(
      `[replay] step ${this._cursor.position}/${this._cursor.total} served for model=${JSON.stringify(modelName)}`,
    );

    if (isStream) {
      res.setHeader('Content-Type', 'text/event-stream');
      res.write(encodeSseEvent(completionToChunkDict(responseDict as Record<string, unknown>, modelName)));
      res.write(SSE_DONE);
      res.end();
    } else {
      res.status(200).json(responseDict);
    }
  }
}

// ---------------------------------------------------------------------------
// ForwardBackend
// ---------------------------------------------------------------------------

export class ForwardBackend {
  private _config: ModelServiceConfig;
  private _recorder: TrajectoryRecorder | null;

  constructor(config: ModelServiceConfig, recorder: TrajectoryRecorder | null = null) {
    this._config = config;
    this._recorder = recorder;
  }

  /** Pick the upstream base URL by model name. */
  _resolveBaseUrl(modelName: string): string {
    if (this._config.proxy_base_url) {
      return this._config.proxy_base_url.replace(/\/+$/, '');
    }

    if (!modelName) {
      throw Object.assign(new Error('Model name is required for routing.'), { statusCode: 400 });
    }

    const rules = this._config.proxy_rules;
    const baseUrl = rules[modelName] ?? rules['default'];
    if (!baseUrl) {
      throw Object.assign(
        new Error(`Model '${modelName}' is not configured and no 'default' rule found.`),
        { statusCode: 400 },
      );
    }

    return baseUrl.replace(/\/+$/, '');
  }

  async serve(
    modelName: string,
    isStream: boolean,
    bodyBytes: Buffer,
    fwdHeaders: Record<string, string>,
    requestDict: Record<string, unknown>,
    res: Response,
  ): Promise<void> {
    const upstreamUrl = `${this._resolveBaseUrl(modelName)}/chat/completions`;
    logger.info(`Routing model ${JSON.stringify(modelName)} to ${upstreamUrl}`);

    const client = axios.create({ timeout: this._config.request_timeout * 1000 });
    const start = Date.now();

    try {
      if (isStream) {
        const { status, headers, data } = await sendWithRetry(
          client,
          upstreamUrl,
          bodyBytes,
          fwdHeaders,
          this._config.retryable_status_codes,
        );

        const upstreamStatus = status;

        // Parse SSE chunks for trajectory recording
        const sseBuffer = data;
        const [chunks] = parseSseDataChunks(sseBuffer);

        // Aggregate chunks into final completion
        let finalDict: Record<string, unknown> | null = null;
        if (chunks.length > 0 && upstreamStatus < 400) {
          try {
            finalDict = aggregateStreamChunks(chunks, modelName);
          } catch (e) {
            logger.warning(`[record] stream aggregation failed: ${e}`);
          }
        }

        // Record before sending response
        if (this._recorder) {
          const recordStatus = upstreamStatus < 400 ? 'success' : 'failure';
          await this._recorder.record({
            request: requestDict,
            response: finalDict,
            status: recordStatus,
            startTime: start,
            endTime: Date.now(),
            error: recordStatus === 'failure' ? `upstream_status=${upstreamStatus}` : undefined,
          });
        }

        // Forward SSE bytes verbatim
        res.setHeader('Content-Type', headers['content-type'] ?? 'text/event-stream');
        res.status(upstreamStatus);
        res.end(data);
      } else {
        const { status, data } = await sendWithRetry(
          client,
          upstreamUrl,
          bodyBytes,
          fwdHeaders,
          this._config.retryable_status_codes,
        );

        const upstreamStatus = status;
        const responseText = data.toString('utf-8');

        let responseDict: Record<string, unknown> | null = null;
        if (responseText) {
          try {
            const parsed = JSON.parse(responseText);
            if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
              responseDict = parsed as Record<string, unknown>;
            }
          } catch {
            // Not valid JSON — record null response
          }
        }

        if (this._recorder) {
          const recordStatus = upstreamStatus < 400 ? 'success' : 'failure';
          await this._recorder.record({
            request: requestDict,
            response: responseDict,
            status: recordStatus,
            startTime: start,
            endTime: Date.now(),
            error: recordStatus === 'failure' ? `upstream_status=${upstreamStatus}` : undefined,
          });
        }

        // Forward bytes verbatim
        const contentType = 'application/json';
        res.status(upstreamStatus);
        res.setHeader('Content-Type', contentType);
        res.end(data);
      }
    } catch (e) {
      const err = e as Error & { statusCode?: number };
      if (err.statusCode) {
        res.status(err.statusCode).json({ detail: err.message });
        return;
      }

      if (this._recorder) {
        await this._recorder.record({
          request: requestDict,
          response: null,
          status: 'failure',
          startTime: start,
          endTime: Date.now(),
          error: `${err.name}: ${err.message}`,
        });
      }

      const statusCode =
        err.message.includes('timeout') || err.message.includes('Timeout') ? 504 : 502;
      res.status(statusCode).json({ detail: `Upstream request failed: ${err.message}` });
    }
  }
}

// ---------------------------------------------------------------------------
// Stream aggregation (minimal replacement for OpenAI SDK)
// ---------------------------------------------------------------------------

/**
 * Aggregate streaming SSE chunks into a final ChatCompletion-like dict.
 *
 * This is a minimal reimplementation of openai's ChatCompletionStreamState
 * to avoid adding the openai npm dependency.
 */
function aggregateStreamChunks(
  chunks: Record<string, unknown>[],
  model: string,
): Record<string, unknown> {
  const deltas: Record<number, Record<string, unknown>> = {};
  let finishReason: string | null = null;
  let chunkId = `chatcmpl-${Date.now()}`;
  let created = Math.floor(Date.now() / 1000);

  for (const chunk of chunks) {
    chunkId = (chunk.id as string) ?? chunkId;
    created = (chunk.created as number) ?? created;

    const choices = (chunk.choices as Array<Record<string, unknown>>) ?? [];
    for (const choice of choices) {
      const idx = (choice.index as number) ?? 0;
      const delta = (choice.delta as Record<string, unknown>) ?? {};

      if (!deltas[idx]) {
        deltas[idx] = {};
      }
      // Merge delta into accumulated message
      const current = deltas[idx]!;
      for (const [key, value] of Object.entries(delta)) {
        if (key === 'tool_calls' && Array.isArray(value)) {
          // Accumulate tool_calls by index
          const existingTCs = (current.tool_calls as Array<Record<string, unknown>>) ?? [];
          for (const tc of value as Array<Record<string, unknown>>) {
            const tcIdx = (tc.index as number) ?? existingTCs.length;
            if (existingTCs[tcIdx]) {
              // Merge: concatenate function arguments
              const existing = existingTCs[tcIdx]!;
              const existingFn = (existing.function as Record<string, unknown>) ?? {};
              const newFn = (tc.function as Record<string, unknown>) ?? {};
              existingTCs[tcIdx] = {
                ...existing,
                ...tc,
                function: {
                  ...existingFn,
                  ...newFn,
                  arguments: ((existingFn.arguments as string) ?? '') + ((newFn.arguments as string) ?? ''),
                },
              };
            } else {
              existingTCs[tcIdx] = tc;
            }
          }
          current.tool_calls = existingTCs;
        } else if (typeof value === 'string' && typeof current[key] === 'string') {
          // Concatenate string deltas (content, role, etc.)
          current[key] = (current[key] as string) + value;
        } else if (value !== null && value !== undefined) {
          current[key] = value;
        }
      }

      if (choice.finish_reason) {
        finishReason = choice.finish_reason as string | null;
      }
    }
  }

  const choices = Object.entries(deltas).map(([idx, delta]) => ({
    index: parseInt(idx, 10),
    message: { role: (delta.role as string) ?? 'assistant', ...delta },
    finish_reason: finishReason,
  }));

  return {
    id: chunkId,
    object: 'chat.completion',
    created,
    model: model as string,
    choices,
  };
}

// ---------------------------------------------------------------------------
// Backend type
// ---------------------------------------------------------------------------

export type CompletionBackend = ReplayBackend | ForwardBackend;

// ---------------------------------------------------------------------------
// Route handler
// ---------------------------------------------------------------------------

/**
 * POST /v1/chat/completions
 *
 * OpenAI-compatible chat completions proxy endpoint.
 * Delegates to the backend attached at startup (replay or forward).
 */
proxyRouter.post('/v1/chat/completions', async (req: Request, res: Response) => {
  let bodyBytes: Buffer;
  try {
    bodyBytes = req.body instanceof Buffer ? req.body : Buffer.from(JSON.stringify(req.body));
  } catch {
    res.status(400).json({ detail: 'Request body is not valid JSON.' });
    return;
  }

  let requestDict: Record<string, unknown>;
  try {
    requestDict =
      typeof req.body === 'object' && req.body !== null
        ? (req.body as Record<string, unknown>)
        : JSON.parse(bodyBytes.toString('utf-8'));
  } catch {
    res.status(400).json({ detail: 'Request body is not valid JSON.' });
    return;
  }

  if (typeof requestDict !== 'object' || requestDict === null) {
    res.status(400).json({ detail: 'Request body must be a JSON object.' });
    return;
  }

  const modelName = (requestDict.model as string) ?? '';
  const isStream = Boolean(requestDict.stream);
  const fwdHeaders = filterHeaders(req.headers);

  const backend = req.app.locals.backend as CompletionBackend;
  if (!backend) {
    res.status(500).json({ detail: 'No backend configured.' });
    return;
  }

  await backend.serve(modelName, isStream, bodyBytes, fwdHeaders, requestDict, res);
});
