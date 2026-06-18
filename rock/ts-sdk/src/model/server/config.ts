/**
 * Configuration for the Model Service server.
 *
 * Mirrors rock/sdk/model/server/config.py.
 */

import { z } from 'zod';
import { join } from 'path';
import { envVars } from '../../env_vars.js';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Log directory for model service data (lazy, reads env var at call time). */
export const LOG_DIR = '/data/logs'; // Default, overridden in practice by env var

/** Default log file for request/response communication. */
export function getLogFile(): string {
  return join(envVars.ROCK_MODEL_SERVICE_DATA_DIR, 'LLMService.log');
}

/** Default trajectory file for recording LLM interactions. */
export function getTrajFile(): string {
  return join(envVars.ROCK_MODEL_SERVICE_DATA_DIR, 'LLMTraj.jsonl');
}

// Eager defaults for backward compat — these match the Python defaults.
export const LOG_FILE: string = getLogFile();
export const TRAJ_FILE: string = getTrajFile();

/** Polling interval for file-based request/response (seconds). */
export const POLLING_INTERVAL_SECONDS = 0.1;

/** Default request timeout (null = infinite). */
export const REQUEST_TIMEOUT: number | null = null;

// Request/response markers — must match Python values character-for-character
// so the TS ModelClient can communicate with the Python model server.
export const REQUEST_START_MARKER = 'LLM_REQUEST_START';
export const REQUEST_END_MARKER = 'LLM_REQUEST_END';
export const RESPONSE_START_MARKER = 'LLM_RESPONSE_START';
export const RESPONSE_END_MARKER = 'LLM_RESPONSE_END';
export const SESSION_END_MARKER = 'SESSION_END';

// ---------------------------------------------------------------------------
// Zod schema
// ---------------------------------------------------------------------------

/**
 * Zod schema for the ModelService configuration.
 *
 * Matches the Python `ModelServiceConfig` Pydantic model in
 * rock/sdk/model/server/config.py.
 */
export const ModelServiceConfigSchema = z
  .object({
    /** Server host address. */
    host: z.string().default('0.0.0.0'),

    /** Server port. */
    port: z.number().int().positive().default(8080),

    /**
     * Direct proxy base URL (e.g. https://your-endpoint.com/v1).
     * Takes precedence over proxy_rules when set.
     */
    proxy_base_url: z.string().nullable().default(null),

    /** Mapping of model names to backend base URLs. */
    proxy_rules: z.record(z.string(), z.string()).default({
      'gpt-3.5-turbo': 'https://api.openai.com/v1',
      default: 'https://api-inference.modelscope.cn/v1',
    }),

    /** HTTP status codes that trigger a retry. Codes not in this list fail immediately. */
    retryable_status_codes: z.array(z.number().int()).default([429, 500]),

    /** Request timeout in seconds. */
    request_timeout: z.number().int().positive().default(120),

    /**
     * Forward mode: path to write the trajectory JSONL.
     * null = use default TRAJ_FILE.
     */
    recording_file: z.string().nullable().default(null),

    /**
     * Replay mode: path to a recorded .jsonl traj file.
     * When set, ReplayBackend serves from recorded responses.
     */
    replay_file: z.string().nullable().default(null),
  })
  .refine(
    (data) => !(data.recording_file && data.replay_file),
    { message: 'recording_file and replay_file are mutually exclusive' },
  );

/** Inferred type for ModelServiceConfig. */
export type ModelServiceConfig = z.infer<typeof ModelServiceConfigSchema>;

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

/**
 * Create a ModelServiceConfig, merging partial overrides with defaults.
 *
 * This is the programmatic API equivalent of `ModelServiceConfig.from_file()`
 * combined with CLI overrides. For YAML file loading, use `loadConfigFromYaml`.
 */
export function createModelServiceConfig(
  overrides?: Partial<ModelServiceConfig>,
): ModelServiceConfig {
  return ModelServiceConfigSchema.parse(overrides ?? {});
}
