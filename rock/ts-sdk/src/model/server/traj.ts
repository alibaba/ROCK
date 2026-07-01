/**
 * Trajectory record + replay for the chat/completions proxy.
 *
 * Two halves around the same JSONL schema (one record per line):
 *
 * - TrajectoryRecorder — invoked by the forward path after each upstream
 *   call (success or failure). Appends a small dict with
 *   request / response / status / response_time / model / stream.
 * - SequentialCursor — loads a JSONL trajectory once at startup;
 *   cursor.next(expectedModel=...) hands out the next record (full
 *   payload dict) and advances.
 *
 * Mirrors rock/sdk/model/server/traj.py.
 */

import * as fs from 'fs';
import * as path from 'path';
import { initLogger } from '../../logger.js';

const logger = initLogger('rock.model.server.traj');

// ---------------------------------------------------------------------------
// TrajectoryExhausted
// ---------------------------------------------------------------------------

/**
 * Raised by SequentialCursor.next when all recorded steps have been served.
 */
export class TrajectoryExhausted extends Error {
  readonly position: number;
  readonly total: number;

  constructor(position: number, total: number) {
    super(
      `trajectory exhausted at step ${position} (total recorded steps=${total})`,
    );
    this.name = 'TrajectoryExhausted';
    this.position = position;
    this.total = total;
  }
}

// ---------------------------------------------------------------------------
// TrajectoryRecorder
// ---------------------------------------------------------------------------

/** Parameters for TrajectoryRecorder.record(). */
export interface TrajectoryRecordParams {
  request: Record<string, unknown>;
  response: Record<string, unknown> | null;
  status: string;
  startTime: number;
  endTime: number;
  error?: string | null | undefined;
}

/**
 * Appends one JSONL line per chat/completions call.
 */
export class TrajectoryRecorder {
  private trajFile: string;

  constructor(trajFile: string) {
    this.trajFile = trajFile;

    // Create parent directory if it doesn't exist
    const dir = path.dirname(trajFile);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }
  }

  /**
   * Record a trajectory entry.
   *
   * Appends a JSON line to the traj file containing request, response,
   * timing, and status information.
   */
  async record(params: TrajectoryRecordParams): Promise<void> {
    const { request, response, status, startTime, endTime, error } = params;
    const rtSeconds = endTime - startTime;

    const payload: Record<string, unknown> = {
      model: request.model,
      stream: Boolean(request.stream),
      status,
      response_time: rtSeconds,
      start_time: startTime,
      end_time: endTime,
      request,
      response,
      error: error ?? null,
    };

    const line = JSON.stringify(payload) + '\n';

    // Synchronous file write wrapped in async for the interface
    await new Promise<void>((resolve, reject) => {
      try {
        fs.appendFileSync(this.trajFile, line, 'utf-8');
        resolve();
      } catch (e) {
        reject(e);
      }
    });
  }
}

// ---------------------------------------------------------------------------
// SequentialCursor
// ---------------------------------------------------------------------------

/**
 * Hands out trajectory records one at a time, in recorded order.
 */
export class SequentialCursor {
  private records: Record<string, unknown>[];
  private _idx: number;

  constructor(records: Record<string, unknown>[]) {
    this.records = records;
    this._idx = 0;
  }

  /**
   * Load a SequentialCursor from a JSONL trajectory file.
   */
  static load(filePath: string): SequentialCursor {
    if (!fs.existsSync(filePath)) {
      throw new Error(`traj file not found: ${filePath}`);
    }

    const records: Record<string, unknown>[] = [];
    const content = fs.readFileSync(filePath, 'utf-8');
    const lines = content.split('\n');

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) {
        continue;
      }
      try {
        records.push(JSON.parse(trimmed));
      } catch {
        // Skip malformed lines silently
      }
    }

    logger.info(
      `[traj-replay] loaded ${records.length} record(s) from ${filePath}`,
    );
    return new SequentialCursor(records);
  }

  /**
   * Return the next trajectory record and advance the cursor.
   *
   * @param expectedModel - If provided, logs a warning when the recorded
   *   model differs from the expected model (but does not throw).
   * @returns The next record.
   * @throws TrajectoryExhausted when all records have been consumed.
   */
  async next(expectedModel?: string): Promise<Record<string, unknown>> {
    if (this._idx >= this.records.length) {
      throw new TrajectoryExhausted(this._idx, this.records.length);
    }

    const record = this.records[this._idx]!;
    this._idx += 1;
    const currentIdx = this._idx - 1;

    if (expectedModel) {
      const recordedModel = record.model;
      if (recordedModel && recordedModel !== expectedModel) {
        logger.warn(
          `[traj-replay] step ${currentIdx} model mismatch: ` +
            `recorded=${JSON.stringify(recordedModel)} requested=${JSON.stringify(expectedModel)}`,
        );
      }
    }

    return record;
  }

  /** Reset the cursor back to the beginning. */
  reset(): void {
    this._idx = 0;
  }

  /** Current position (number of records consumed). */
  get position(): number {
    return this._idx;
  }

  /** Total number of loaded records. */
  get total(): number {
    return this.records.length;
  }
}
