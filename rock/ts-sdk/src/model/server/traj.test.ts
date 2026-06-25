/**
 * Tests for model/server/traj.ts
 */

import { existsSync, mkdtempSync, rmSync, readFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { SequentialCursor, TrajectoryRecorder, TrajectoryExhausted } from './traj.js';
import { initLogger } from '../../logger.js';

describe('SequentialCursor', () => {
  const sampleRecords = [
    { model: 'gpt-4', stream: false, status: 'success', request: { model: 'gpt-4' }, response: { choices: [{ message: { content: 'Hello' } }] } },
    { model: 'gpt-3.5', stream: true, status: 'success', request: { model: 'gpt-3.5' }, response: { choices: [{ message: { content: 'World' } }] } },
  ];

  it('loads records from a JSONL file', () => {
    const dir = mkdtempSync(join(tmpdir(), 'rock-trajs-'));
    const file = join(dir, 'test.jsonl');
    try {
      const fs = require('fs');
      fs.writeFileSync(
        file,
        sampleRecords.map((r) => JSON.stringify(r)).join('\n') + '\n',
      );

      const cursor = SequentialCursor.load(file);
      expect(cursor.total).toBe(2);
      expect(cursor.position).toBe(0);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('throws FileNotFoundError when file does not exist', () => {
    expect(() => SequentialCursor.load('/nonexistent/path.jsonl')).toThrow();
  });

  it('iterates through records with next()', async () => {
    const cursor = new SequentialCursor(sampleRecords);

    const r1 = await cursor.next();
    expect(r1.model).toBe('gpt-4');

    const r2 = await cursor.next();
    expect(r2.model).toBe('gpt-3.5');

    expect(cursor.position).toBe(2);
  });

  it('throws TrajectoryExhausted when past the end', async () => {
    const cursor = new SequentialCursor(sampleRecords);
    await cursor.next();
    await cursor.next();

    await expect(cursor.next()).rejects.toThrow(TrajectoryExhausted);
  });

  it('TrajectoryExhausted has position and total', async () => {
    const cursor = new SequentialCursor(sampleRecords);
    await cursor.next();
    await cursor.next();

    try {
      await cursor.next();
      fail('should have thrown');
    } catch (e) {
      expect(e).toBeInstanceOf(TrajectoryExhausted);
      if (e instanceof TrajectoryExhausted) {
        expect((e as TrajectoryExhausted).position).toBe(2);
        expect((e as TrajectoryExhausted).total).toBe(2);
      }
    }
  });

  it('resets position to 0', async () => {
    const cursor = new SequentialCursor(sampleRecords);
    await cursor.next();
    expect(cursor.position).toBe(1);

    cursor.reset();
    expect(cursor.position).toBe(0);

    const r = await cursor.next();
    expect(r.model).toBe('gpt-4');
  });

  it('warns on model mismatch', async () => {
    const cursor = new SequentialCursor(sampleRecords);
    // Requested model differs from recorded, should not throw but should log warning
    const r = await cursor.next('different-model');
    expect(r.model).toBe('gpt-4');
  });

  it('empty file loads 0 records', () => {
    const dir = mkdtempSync(join(tmpdir(), 'rock-trajs-'));
    const file = join(dir, 'empty.jsonl');
    try {
      const fs = require('fs');
      fs.writeFileSync(file, '');
      const cursor = SequentialCursor.load(file);
      expect(cursor.total).toBe(0);
      expect(cursor.position).toBe(0);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('skips blank lines in JSONL', () => {
    const dir = mkdtempSync(join(tmpdir(), 'rock-trajs-'));
    const file = join(dir, 'blanks.jsonl');
    try {
      const fs = require('fs');
      fs.writeFileSync(
        file,
        '\n\n' + JSON.stringify({ model: 'gpt-4' }) + '\n\n' + JSON.stringify({ model: 'gpt-3.5' }) + '\n\n',
      );
      const cursor = SequentialCursor.load(file);
      expect(cursor.total).toBe(2);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('TrajectoryRecorder', () => {
  it('records a line to the traj file', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'rock-trajs-'));
    const trajFile = join(dir, 'traj.jsonl');
    try {
      const recorder = new TrajectoryRecorder(trajFile);
      await recorder.record({
        request: { model: 'gpt-4' },
        response: { choices: [{ message: { content: 'response' } }] },
        status: 'success',
        startTime: 1000,
        endTime: 1500,
      });

      const content = readFileSync(trajFile, 'utf-8');
      const parsed = JSON.parse(content.trim());
      expect(parsed.model).toBe('gpt-4');
      expect(parsed.status).toBe('success');
      expect(parsed.response_time).toBe(500);
      expect(parsed.request).toEqual({ model: 'gpt-4' });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('records error info', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'rock-trajs-'));
    const trajFile = join(dir, 'traj.jsonl');
    try {
      const recorder = new TrajectoryRecorder(trajFile);
      await recorder.record({
        request: { model: 'gpt-4' },
        response: null,
        status: 'failure',
        startTime: 1000,
        endTime: 1005,
        error: 'timeout: ConnectTimeout',
      });

      const content = readFileSync(trajFile, 'utf-8');
      const parsed = JSON.parse(content.trim());
      expect(parsed.status).toBe('failure');
      expect(parsed.response).toBeNull();
      expect(parsed.error).toBe('timeout: ConnectTimeout');
      expect(parsed.response_time).toBe(5);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('appends multiple records', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'rock-trajs-'));
    const trajFile = join(dir, 'traj.jsonl');
    try {
      const recorder = new TrajectoryRecorder(trajFile);
      await recorder.record({
        request: { model: 'a' },
        response: {},
        status: 'success',
        startTime: 0,
        endTime: 1,
      });
      await recorder.record({
        request: { model: 'b' },
        response: {},
        status: 'success',
        startTime: 0,
        endTime: 2,
      });

      const lines = readFileSync(trajFile, 'utf-8').trim().split('\n');
      expect(lines.length).toBe(2);
      expect(JSON.parse(lines[0]!).model).toBe('a');
      expect(JSON.parse(lines[1]!).model).toBe('b');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('creates parent directory if it does not exist', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'rock-trajs-'));
    const nestedFile = join(dir, 'sub', 'deep', 'traj.jsonl');
    try {
      const recorder = new TrajectoryRecorder(nestedFile);
      await recorder.record({
        request: { model: 'test' },
        response: { ok: true },
        status: 'success',
        startTime: 0,
        endTime: 1,
      });

      expect(existsSync(nestedFile)).toBe(true);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('sets stream flag from request', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'rock-trajs-'));
    const trajFile = join(dir, 'traj.jsonl');
    try {
      const recorder = new TrajectoryRecorder(trajFile);
      await recorder.record({
        request: { model: 'gpt-4', stream: true },
        response: null,
        status: 'success',
        startTime: 0,
        endTime: 1,
      });

      const content = readFileSync(trajFile, 'utf-8');
      const parsed = JSON.parse(content.trim());
      expect(parsed.stream).toBe(true);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
