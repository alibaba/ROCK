/**
 * Tests for model/server/file_handler.ts
 */

import { mkdtempSync, rmSync, readFileSync, existsSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { FileHandler } from './file_handler.js';

describe('FileHandler', () => {
  let tmpDir: string;
  let logFile: string;

  beforeEach(() => {
    tmpDir = mkdtempSync(join(tmpdir(), 'rock-fh-'));
    logFile = join(tmpDir, 'test.log');
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  describe('writeRequest', () => {
    it('writes a request line to the log file', () => {
      const handler = new FileHandler(logFile);
      handler.writeRequest({ model: 'gpt-4', messages: [{ role: 'user', content: 'Hi' }] }, 1);

      expect(existsSync(logFile)).toBe(true);
      const content = readFileSync(logFile, 'utf-8');
      expect(content).toContain('LLM_REQUEST_START');
      expect(content).toContain('LLM_REQUEST_END');
      expect(content).toContain('gpt-4');
      expect(content).toContain('"index":1');
    });

    it('writes multiple requests sequentially', () => {
      const handler = new FileHandler(logFile);
      handler.writeRequest({ model: 'a' }, 1);
      handler.writeRequest({ model: 'b' }, 2);

      const content = readFileSync(logFile, 'utf-8');
      const lines = content.trim().split('\n');
      expect(lines.length).toBe(2);
      expect(lines[0]).toContain('"model":"a"');
      expect(lines[1]).toContain('"model":"b"');
    });
  });

  describe('writeSessionEnd', () => {
    it('writes SESSION_END marker', () => {
      const handler = new FileHandler(logFile);
      handler.writeSessionEnd();

      const content = readFileSync(logFile, 'utf-8');
      expect(content).toContain('SESSION_END');
    });
  });

  describe('pollForResponse', () => {
    it('resolves when a matching response is found', async () => {
      const handler = new FileHandler(logFile);

      // Write a response first, then poll
      const responseLine =
        'LLM_RESPONSE_START{"status":"ok","content":"Hello world"}LLM_RESPONSE_END{"timestamp":0,"index":1}\n';
      const fs = require('fs');
      fs.writeFileSync(logFile, responseLine);

      const result = await handler.pollForResponse(1, 2); // 2s timeout
      expect(result).toEqual({ status: 'ok', content: 'Hello world' });
    });

    it('returns null on session end', async () => {
      const handler = new FileHandler(logFile);

      const fs = require('fs');
      fs.writeFileSync(logFile, 'SESSION_END\n');

      const result = await handler.pollForResponse(1, 2);
      expect(result).toBeNull();
    });

    it('throws on timeout with no matching response', async () => {
      const handler = new FileHandler(logFile);

      // Write a response with wrong index
      const fs = require('fs');
      fs.writeFileSync(logFile, 'LLM_RESPONSE_START{"x":1}LLM_RESPONSE_END{"index":99}\n');

      await expect(handler.pollForResponse(1, 1)).rejects.toThrow(/timed out/);
    }, 5000);

    it('skips responses with non-matching index', async () => {
      const handler = new FileHandler(logFile);

      const fs = require('fs');
      // Pre-write a response for index 99, then append matching one
      fs.writeFileSync(
        logFile,
        'LLM_RESPONSE_START{"wrong":true}LLM_RESPONSE_END{"index":99}\n' +
          'LLM_RESPONSE_START{"right":true}LLM_RESPONSE_END{"index":1}\n',
      );

      const result = await handler.pollForResponse(1, 2);
      expect(result).toEqual({ right: true });
    });
  });
});
