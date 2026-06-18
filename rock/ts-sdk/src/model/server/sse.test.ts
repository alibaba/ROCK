/**
 * Tests for model/server/sse.ts
 */

import { parseSseDataChunks, completionToChunkDict, encodeSseEvent, SSE_DONE } from './sse.js';

/** Helper to narrow completionToChunkDict return for testing. */
interface ChunkDict {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: Array<{
    index: number;
    delta: Record<string, unknown>;
    finish_reason: string | null;
    logprobs: unknown | null;
  }>;
}

function asChunk(d: Record<string, unknown>): ChunkDict {
  return d as unknown as ChunkDict;
}

describe('SSE_DONE', () => {
  it('is the bytes for data: [DONE]\\n\\n', () => {
    expect(SSE_DONE.toString()).toBe('data: [DONE]\n\n');
  });
});

describe('parseSseDataChunks', () => {
  it('extracts a single complete SSE event', () => {
    const buffer = Buffer.from('data: {"foo":"bar"}\n\n');
    const [chunks, leftover] = parseSseDataChunks(buffer);

    expect(chunks).toEqual([{ foo: 'bar' }]);
    expect(leftover.length).toBe(0);
  });

  it('extracts multiple SSE events from one buffer', () => {
    const buffer = Buffer.from(
      'data: {"a":1}\n\ndata: {"b":2}\n\n',
    );
    const [chunks, leftover] = parseSseDataChunks(buffer);

    expect(chunks).toEqual([{ a: 1 }, { b: 2 }]);
    expect(leftover.length).toBe(0);
  });

  it('returns leftover bytes for an incomplete event', () => {
    const buffer = Buffer.from('data: {"partial":');
    const [chunks, leftover] = parseSseDataChunks(buffer);

    expect(chunks).toEqual([]);
    expect(leftover.toString()).toBe('data: {"partial":');
  });

  it('accumulates across calls: leftover + new bytes', () => {
    // First call with partial data
    const buf1 = Buffer.from('data: {"val":1}\n\ndata: {"half');
    const [chunks1, leftover1] = parseSseDataChunks(buf1);
    expect(chunks1).toEqual([{ val: 1 }]);
    expect(leftover1.toString()).toBe('data: {"half');

    // Second call: leftover + remaining bytes
    const buf2 = Buffer.from('way":2}\n\ndata: {"end":3}\n\n');
    const [chunks2, leftover2] = parseSseDataChunks(Buffer.concat([leftover1, buf2]));
    expect(chunks2).toEqual([{ halfway: 2 }, { end: 3 }]);
    expect(leftover2.length).toBe(0);
  });

  it('skips data: [DONE] events', () => {
    const buffer = Buffer.from(
      'data: {"a":1}\n\ndata: [DONE]\n\ndata: {"b":2}\n\n',
    );
    const [chunks, leftover] = parseSseDataChunks(buffer);

    expect(chunks).toEqual([{ a: 1 }, { b: 2 }]);
    expect(leftover.length).toBe(0);
  });

  it('skips empty data: lines', () => {
    const buffer = Buffer.from(
      'data: \n\ndata: {"valid":true}\n\n',
    );
    const [chunks, leftover] = parseSseDataChunks(buffer);

    expect(chunks).toEqual([{ valid: true }]);
    expect(leftover.length).toBe(0);
  });

  it('skips lines not starting with data:', () => {
    const buffer = Buffer.from(
      'event: message\ndata: {"x":1}\n\n',
    );
    const [chunks, leftover] = parseSseDataChunks(buffer);

    // "event: message" is not a data line, only the "data:" line is parsed
    expect(chunks).toEqual([{ x: 1 }]);
    expect(leftover.length).toBe(0);
  });

  it('skips malformed JSON silently', () => {
    const buffer = Buffer.from(
      'data: not-json\n\ndata: {"ok":true}\n\n',
    );
    const [chunks, leftover] = parseSseDataChunks(buffer);

    expect(chunks).toEqual([{ ok: true }]);
    expect(leftover.length).toBe(0);
  });

  it('handles multi-line SSE events (only data: line)', () => {
    const buffer = Buffer.from(
      'data: line1\ndata: line2\n\n',
    );
    const [chunks, leftover] = parseSseDataChunks(buffer);

    // Each data: line is parsed independently; malformed JSON is skipped silently
    expect(chunks).toEqual([]);
    expect(leftover.length).toBe(0);
  });
});

describe('completionToChunkDict', () => {
  it('converts a simple completion to a chunk dict', () => {
    const response = {
      id: 'chatcmpl-123',
      object: 'chat.completion',
      created: 1700000000,
      model: 'gpt-4',
      choices: [
        {
          index: 0,
          message: { role: 'assistant', content: 'Hello!' },
          finish_reason: 'stop',
        },
      ],
    };

    const chunk = asChunk(completionToChunkDict(response, 'gpt-4'));

    expect(chunk.id).toBe('chatcmpl-123');
    expect(chunk.object).toBe('chat.completion.chunk');
    expect(chunk.created).toBe(1700000000);
    expect(chunk.model).toBe('gpt-4');
    expect(chunk.choices[0]!.delta).toEqual({ role: 'assistant', content: 'Hello!' });
    expect(chunk.choices[0]!.finish_reason).toBe('stop');
    expect(chunk.choices[0]!.index).toBe(0);
  });

  it('synthesizes id and created when missing', () => {
    const response = {
      model: 'gpt-4',
      choices: [{ message: { content: 'Hi' } }],
    };

    const chunk = asChunk(completionToChunkDict(response, 'gpt-4'));

    expect(chunk.id).toMatch(/^chatcmpl-/);
    expect(typeof chunk.created).toBe('number');
    expect(chunk.model).toBe('gpt-4');
  });

  it('uses the model parameter when response.model is missing', () => {
    const response = {
      choices: [{ message: { content: 'Hi' } }],
    };

    const chunk = asChunk(completionToChunkDict(response, 'custom-model'));

    expect(chunk.model).toBe('custom-model');
  });

  it('injects index into tool_calls items when missing', () => {
    const response = {
      model: 'gpt-4',
      choices: [
        {
          index: 0,
          message: {
            role: 'assistant',
            tool_calls: [
              { id: 'call_1', type: 'function', function: { name: 'foo', arguments: '{}' } },
              { id: 'call_2', type: 'function', function: { name: 'bar', arguments: '{}' } },
            ],
          },
          finish_reason: 'tool_calls',
        },
      ],
    };

    const chunk = asChunk(completionToChunkDict(response, 'gpt-4'));

    const tc = chunk.choices[0]!.delta.tool_calls as Array<{ index: number }>;
    expect(tc[0]!.index).toBe(0);
    expect(tc[1]!.index).toBe(1);
  });

  it('preserves existing index in tool_calls', () => {
    const response = {
      model: 'gpt-4',
      choices: [
        {
          message: {
            tool_calls: [{ index: 5, id: 'call_x', type: 'function', function: { name: 'f', arguments: '{}' } }],
          },
        },
      ],
    };

    const chunk = asChunk(completionToChunkDict(response, 'gpt-4'));

    const tc = chunk.choices[0]!.delta.tool_calls as Array<{ index: number }>;
    expect(tc[0]!.index).toBe(5);
  });

  it('handles empty choices array', () => {
    const response = { model: 'gpt-4', choices: [] };
    const chunk = asChunk(completionToChunkDict(response, 'gpt-4'));

    expect(chunk.choices).toEqual([]);
  });

  it('handles missing choices key', () => {
    const response = { model: 'gpt-4' };
    const chunk = asChunk(completionToChunkDict(response, 'gpt-4'));

    expect(chunk.choices).toEqual([]);
  });

  it('renames message to delta without mutation', () => {
    const response = {
      model: 'gpt-4',
      choices: [{ message: { role: 'assistant', content: 'Test' } } as const],
    };

    const chunk = asChunk(completionToChunkDict(response, 'gpt-4'));

    // Original should be unmodified
    expect(response.choices[0]!.message).toBeDefined();
    // Chunk should use 'delta'
    expect(chunk.choices[0]!.delta).toBeDefined();
    // Message key should not exist in delta
    expect(chunk.choices[0]!).not.toHaveProperty('message');
  });
});

describe('encodeSseEvent', () => {
  it('encodes a dict as data: <json>\\n\\n', () => {
    const result = encodeSseEvent({ hello: 'world' });
    expect(result.toString()).toBe('data: {"hello":"world"}\n\n');
  });

  it('encodes non-ASCII characters', () => {
    const result = encodeSseEvent({ greeting: '你好' });
    const decoded = result.toString();
    expect(decoded).toContain('你好');
    expect(decoded.startsWith('data: ')).toBe(true);
    expect(decoded.endsWith('\n\n')).toBe(true);
  });
});
