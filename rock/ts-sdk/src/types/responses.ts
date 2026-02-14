/**
 * Response types
 * Note: All field names use snake_case to match Python SDK and API response
 */

import { z } from 'zod';
import { Codes } from './codes.js';

/**
 * Base sandbox response
 */
export const SandboxResponseSchema = z.object({
  code: z.nativeEnum(Codes).optional(),
  exit_code: z.number().optional(),
  failure_reason: z.string().optional(),
});

export type SandboxResponse = z.infer<typeof SandboxResponseSchema>;

/**
 * Is alive response
 */
export const IsAliveResponseSchema = z.object({
  is_alive: z.boolean(),
  message: z.string().default(''),
});

export type IsAliveResponse = z.infer<typeof IsAliveResponseSchema>;

/**
 * Sandbox status response
 * Note: API returns snake_case fields
 */
export const SandboxStatusResponseSchema = z.object({
  sandbox_id: z.string().optional(),
  status: z.record(z.unknown()).optional(),
  port_mapping: z.record(z.unknown()).optional(),
  host_name: z.string().optional(),
  host_ip: z.string().optional(),
  is_alive: z.boolean().default(true),
  image: z.string().optional(),
  gateway_version: z.string().optional(),
  swe_rex_version: z.string().optional(),
  user_id: z.string().optional(),
  experiment_id: z.string().optional(),
  namespace: z.string().optional(),
  cpus: z.number().optional(),
  memory: z.string().optional(),
  state: z.unknown().optional(),
});

export type SandboxStatusResponse = z.infer<typeof SandboxStatusResponseSchema>;

/**
 * Command execution response
 */
export const CommandResponseSchema = z.object({
  stdout: z.string().default(''),
  stderr: z.string().default(''),
  exit_code: z.number().optional(),
});

export type CommandResponse = z.infer<typeof CommandResponseSchema>;

/**
 * Write file response
 */
export const WriteFileResponseSchema = z.object({
  success: z.boolean().default(false),
  message: z.string().default(''),
});

export type WriteFileResponse = z.infer<typeof WriteFileResponseSchema>;

/**
 * Read file response
 */
export const ReadFileResponseSchema = z.object({
  content: z.string().default(''),
});

export type ReadFileResponse = z.infer<typeof ReadFileResponseSchema>;

/**
 * Upload response
 */
export const UploadResponseSchema = z.object({
  success: z.boolean().default(false),
  message: z.string().default(''),
  file_name: z.string().optional(),
});

export type UploadResponse = z.infer<typeof UploadResponseSchema>;

/**
 * Bash observation (execution result)
 */
export const ObservationSchema = z.object({
  output: z.string().default(''),
  exit_code: z.number().optional(),
  failure_reason: z.string().default(''),
  expect_string: z.string().default(''),
});

export type Observation = z.infer<typeof ObservationSchema>;

/**
 * Create session response
 */
export const CreateSessionResponseSchema = z.object({
  output: z.string().default(''),
  session_type: z.literal('bash').default('bash'),
});

export type CreateSessionResponse = z.infer<typeof CreateSessionResponseSchema>;

/**
 * Close session response
 */
export const CloseSessionResponseSchema = z.object({
  session_type: z.literal('bash').default('bash'),
});

export type CloseSessionResponse = z.infer<typeof CloseSessionResponseSchema>;

/**
 * Close response
 */
export const CloseResponseSchema = z.object({});

export type CloseResponse = z.infer<typeof CloseResponseSchema>;

/**
 * Chown response
 */
export const ChownResponseSchema = z.object({
  success: z.boolean().default(false),
  message: z.string().default(''),
});

export type ChownResponse = z.infer<typeof ChownResponseSchema>;

/**
 * Chmod response
 */
export const ChmodResponseSchema = z.object({
  success: z.boolean().default(false),
  message: z.string().default(''),
});

export type ChmodResponse = z.infer<typeof ChmodResponseSchema>;

/**
 * Execute bash session response
 */
export const ExecuteBashSessionResponseSchema = z.object({
  success: z.boolean().default(false),
  message: z.string().default(''),
});

export type ExecuteBashSessionResponse = z.infer<typeof ExecuteBashSessionResponseSchema>;

/**
 * OSS setup response
 */
export const OssSetupResponseSchema = z.object({
  success: z.boolean().default(false),
  message: z.string().default(''),
});

export type OssSetupResponse = z.infer<typeof OssSetupResponseSchema>;
