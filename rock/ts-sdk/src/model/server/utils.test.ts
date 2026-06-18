/**
 * Tests for model/server/utils.ts
 */

import { writeTraj, MODEL_SERVICE_REQUEST_RT, MODEL_SERVICE_REQUEST_COUNT } from './utils.js';

describe('MODEL_SERVICE_REQUEST_RT', () => {
  it('has the expected value', () => {
    expect(MODEL_SERVICE_REQUEST_RT).toBe('model_service.request.rt');
  });
});

describe('MODEL_SERVICE_REQUEST_COUNT', () => {
  it('has the expected value', () => {
    expect(MODEL_SERVICE_REQUEST_COUNT).toBe('model_service.request.count');
  });
});

describe('writeTraj', () => {
  it('writes a JSONL line to a temp file', async () => {
    const fs = await import('fs');
    const os = await import('os');
    const path = await import('path');

    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rock-trajs-'));
    const trajFile = path.join(tmpDir, 'LLMTraj.jsonl');

    // Set up env to use append mode
    const oldDir = process.env.ROCK_MODEL_SERVICE_DATA_DIR;
    process.env.ROCK_MODEL_SERVICE_DATA_DIR = tmpDir;

    try {
      writeTraj({ request: { model: 'test' }, response: { status: 'ok' } });

      const content = fs.readFileSync(trajFile, 'utf-8');
      const parsed = JSON.parse(content.trim());
      expect(parsed.request).toEqual({ model: 'test' });
      expect(parsed.response).toEqual({ status: 'ok' });
    } finally {
      process.env.ROCK_MODEL_SERVICE_DATA_DIR = oldDir;
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  it('appends when ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE is true', async () => {
    const fs = await import('fs');
    const os = await import('os');
    const path = await import('path');

    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rock-trajs-'));
    const trajFile = path.join(tmpDir, 'LLMTraj.jsonl');

    const oldDir = process.env.ROCK_MODEL_SERVICE_DATA_DIR;
    const oldAppend = process.env.ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE;
    process.env.ROCK_MODEL_SERVICE_DATA_DIR = tmpDir;
    process.env.ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE = 'true';

    try {
      writeTraj({ first: 1 });
      writeTraj({ second: 2 });

      const lines = fs.readFileSync(trajFile, 'utf-8').trim().split('\n');
      expect(lines.length).toBe(2);
      expect(JSON.parse(lines[0]!)).toEqual({ first: 1 });
      expect(JSON.parse(lines[1]!)).toEqual({ second: 2 });
    } finally {
      process.env.ROCK_MODEL_SERVICE_DATA_DIR = oldDir;
      process.env.ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE = oldAppend;
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });
});
