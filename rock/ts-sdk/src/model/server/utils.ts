/**
 * Utility functions for the model server.
 *
 * Mirrors rock/sdk/model/server/utils.py.
 */

import { existsSync, mkdirSync, appendFileSync, writeFileSync } from 'fs';
import { join } from 'path';
import { envVars } from '../../env_vars.js';

// ---------------------------------------------------------------------------
// Metric name constants
// ---------------------------------------------------------------------------

export const MODEL_SERVICE_REQUEST_RT = 'model_service.request.rt';
export const MODEL_SERVICE_REQUEST_COUNT = 'model_service.request.count';

// ---------------------------------------------------------------------------
// writeTraj
// ---------------------------------------------------------------------------

/**
 * Write traj data to file in JSONL format.
 *
 * The file path is derived from ROCK_MODEL_SERVICE_DATA_DIR / 'LLMTraj.jsonl'.
 * In append mode (ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE='true'), new lines are
 * appended. Otherwise, the file is overwritten.
 */
export function writeTraj(data: Record<string, unknown>): void {
  const logDir = envVars.ROCK_MODEL_SERVICE_DATA_DIR;
  const trajFile = join(logDir, 'LLMTraj.jsonl');
  const append = envVars.ROCK_MODEL_SERVICE_TRAJ_APPEND_MODE;

  if (!existsSync(logDir)) {
    mkdirSync(logDir, { recursive: true });
  }

  const line = JSON.stringify(data) + '\n';

  if (append) {
    appendFileSync(trajFile, line, 'utf-8');
  } else {
    writeFileSync(trajFile, line, 'utf-8');
  }
}
