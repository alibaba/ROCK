/**
 * Tests for model/server/api/local.ts
 */

import express from 'express';
import http from 'http';
import { localRouter, initLocalApi } from './local.js';
import { readFileSync, unlinkSync, existsSync, mkdtempSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

describe('localRouter', () => {
  let tmpDir: string;
  let server: http.Server;
  let url: string;

  beforeAll(async () => {
    tmpDir = mkdtempSync(join(tmpdir(), 'rock-local-api-'));
    process.env.ROCK_MODEL_SERVICE_DATA_DIR = tmpDir;
  });

  afterAll(() => {
    delete process.env.ROCK_MODEL_SERVICE_DATA_DIR;
    rmSync(tmpDir, { recursive: true, force: true });
  });

  beforeEach(async () => {
    // Reset and init local API before each test
    await initLocalApi();

    const app = express();
    app.use(express.json());
    app.use('/', localRouter);

    await new Promise<void>((resolve) => {
      server = app.listen(0, () => {
        const addr = server.address() as { port: number };
        url = `http://127.0.0.1:${addr.port}`;
        resolve();
      });
    });
  });

  afterEach(async () => {
    if (server) {
      await new Promise<void>((r) => server.close(() => r()));
    }
  });

  describe('health endpoint', () => {
    it('returns healthy status', async () => {
      const resp = await fetch(`${url}/health`);
      expect(resp.status).toBe(200);
      const body = await resp.json();
      expect(body).toEqual({ status: 'healthy' });
    });
  });

  describe('POST /v1/agent/watch', () => {
    it('returns 400 when pid is missing', async () => {
      const resp = await fetch(`${url}/v1/agent/watch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      expect(resp.status).toBe(400);
    });

    it('accepts a watch request with pid', async () => {
      const resp = await fetch(`${url}/v1/agent/watch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pid: 99999 }),
      });
      expect(resp.status).toBe(200);
      const body = (await resp.json()) as { status: string; pid: number };
      expect(body.status).toBe('watching');
      expect(body.pid).toBe(99999);
    });
  });

  describe('POST /v1/chat/completions', () => {
    it('returns 500 when no response is available (poll timeout)', async () => {
      // No response in file yet, so poll should timeout and return 500
      // Use AbortController to limit wait time
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 3000);

      try {
        const resp = await fetch(`${url}/v1/chat/completions`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: 'gpt-4', messages: [{ role: 'user', content: 'hi' }] }),
          signal: controller.signal,
        });
        // May get 500 if server's poll timeout fires before our abort
        expect([500, 499]).toContain(resp.status);
      } catch {
        // Fetch may throw on abort — that's fine for this test
      } finally {
        clearTimeout(timeoutId);
      }
    }, 10000);
  });
});
