/**
 * SSE codec utilities for the chat/completions proxy.
 *
 * Three pure helpers, no LLM SDK dependencies:
 *
 * - parseSseDataChunks — incremental SSE byte stream -> list of decoded
 *   data: payload dicts (used by the forward path to feed chunks into the
 *   stream-state aggregator while bytes pass through verbatim to the client).
 * - completionToChunkDict — convert a non-streaming chat.completion response
 *   into a single chat.completion.chunk dict, by renaming message -> delta.
 *   Used by the replay path's streaming output.
 * - encodeSseEvent — encode a payload dict as data: <json>\n\n bytes (one SSE event).
 *
 * Mirrors rock/sdk/model/server/sse.py.
 */

import { randomUUID } from 'crypto';

// ---------------------------------------------------------------------------
// Terminal SSE event
// ---------------------------------------------------------------------------

/** Terminal SSE event sent at the end of a chat/completions stream. */
export const SSE_DONE: Buffer = Buffer.from('data: [DONE]\n\n');

// ---------------------------------------------------------------------------
// parseSseDataChunks
// ---------------------------------------------------------------------------

/**
 * Extract complete SSE events from a (possibly partial) byte buffer.
 *
 * Returns `[chunks, leftover]`: the parsed `data:` JSON payload dicts and
 * the bytes that did not yet form a complete event (`\n\n`-terminated).
 *
 * - `data: [DONE]` is skipped (terminal marker, has no JSON payload).
 * - Lines that don't start with `data:` (event:/id:/blank) are ignored.
 * - Malformed JSON in a `data:` line is silently skipped.
 */
export function parseSseDataChunks(
  buffer: Buffer,
): [Record<string, unknown>[], Buffer] {
  const chunks: Record<string, unknown>[] = [];
  let leftover = buffer;

  while (leftover.includes('\n\n')) {
    const splitIdx = leftover.indexOf('\n\n');
    const eventBytes = leftover.subarray(0, splitIdx);
    leftover = leftover.subarray(splitIdx + 2);

    const eventStr = eventBytes.toString('utf-8');
    for (const rawLine of eventStr.split('\n')) {
      const line = rawLine.trim();
      if (!line.startsWith('data:')) {
        continue;
      }
      const payload = line.slice('data:'.length).trim();
      if (!payload || payload === '[DONE]') {
        continue;
      }
      try {
        chunks.push(JSON.parse(payload));
      } catch {
        // Malformed JSON — silently skip
      }
    }
  }

  return [chunks, leftover];
}

// ---------------------------------------------------------------------------
// completionToChunkDict
// ---------------------------------------------------------------------------

/**
 * Convert a recorded chat.completion dict into a single
 * chat.completion.chunk dict, suitable for re-streaming.
 *
 * Only message -> delta is renamed; every other field (including
 * provider-specific extras like reasoning_content inside the message)
 * flows through unchanged. id / created are synthesized when missing.
 *
 * tool_calls items get a positional index injected if missing — the
 * OpenAI streaming spec requires it on chunk deltas.
 */
export function completionToChunkDict(
  response: Record<string, unknown>,
  model: string,
): Record<string, unknown> {
  const choicesIn = (response.choices as Record<string, unknown>[]) ?? [];
  const choicesOut: Record<string, unknown>[] = [];

  for (const choice of choicesIn) {
    const delta = { ...(choice.message as Record<string, unknown> ?? {}) };

    if (Array.isArray(delta.tool_calls) && delta.tool_calls.length > 0) {
      delta.tool_calls = (delta.tool_calls as Record<string, unknown>[]).map(
        (tc, i) => ({ index: tc.index ?? i, ...tc }),
      );
    }

    choicesOut.push({
      index: choice.index ?? 0,
      delta,
      finish_reason: choice.finish_reason ?? null,
      logprobs: choice.logprobs ?? null,
    });
  }

  return {
    id: response.id ?? `chatcmpl-${randomUUID()}`,
    object: 'chat.completion.chunk',
    created: response.created ?? Math.floor(Date.now() / 1000),
    model: response.model ?? model,
    choices: choicesOut,
  };
}

// ---------------------------------------------------------------------------
// encodeSseEvent
// ---------------------------------------------------------------------------

/**
 * Encode a JSON payload as one SSE `data:` event (terminated by `\n\n`).
 */
export function encodeSseEvent(data: Record<string, unknown>): Buffer {
  return Buffer.from(`data: ${JSON.stringify(data)}\n\n`, 'utf-8');
}
