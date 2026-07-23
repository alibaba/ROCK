/**
 * Response types
 * All field names use camelCase for TypeScript convention
 * HTTP layer automatically converts from API snake_case
 */

import { z } from 'zod';
import { Codes } from './codes.js';

/**
 * Sandbox state enum
 */
export const SandboxState = {
  PENDING: 'pending',
  RUNNING: 'running',
  STOPPED: 'stopped',
  ARCHIVING: 'archiving',
  ARCHIVED: 'archived',
  DELETED: 'deleted',
} as const;

export type SandboxState = (typeof SandboxState)[keyof typeof SandboxState];

/**
 * State transition record
 */
export const StateTransitionRecordSchema = z.object({
  fromState: z.string(),
  toState: z.string(),
  event: z.string(),
  timestamp: z.string(),
});

export type StateTransitionRecord = z.infer<typeof StateTransitionRecordSchema>;

/**
 * Base sandbox response
 */
export const SandboxResponseSchema = z.object({
  code: z.nativeEnum(Codes).optional(),
  exitCode: z.number().optional(),
  failureReason: z.string().optional(),
});

export type SandboxResponse = z.infer<typeof SandboxResponseSchema>;

/**
 * Is alive response
 */
export const IsAliveResponseSchema = z.object({
  isAlive: z.boolean(),
  message: z.string().default(''),
});

export type IsAliveResponse = z.infer<typeof IsAliveResponseSchema>;

/**
 * Sandbox status response
 */
export const SandboxStatusResponseSchema = z.object({
  sandboxId: z.string().optional(),
  status: z.record(z.unknown()).optional(),
  portMapping: z.record(z.unknown()).optional(),
  hostName: z.string().optional(),
  hostIp: z.string().optional(),
  isAlive: z.boolean().default(true),
  image: z.string().optional(),
  gatewayVersion: z.string().optional(),
  sweRexVersion: z.string().optional(),
  userId: z.string().optional(),
  experimentId: z.string().optional(),
  namespace: z.string().optional(),
  cpus: z.number().optional(),
  memory: z.string().optional(),
  disk: z.string().nullable().default(null),
  diskLimitRootfs: z.string().nullable().default(null),
  state: z.enum(['pending', 'running', 'stopped', 'archiving', 'archived', 'deleted']).nullable().default(null),
  startTime: z.string().nullable().default(null),
  stopTime: z.string().nullable().default(null),
  createTime: z.string().nullable().default(null),
  stateHistory: z.array(StateTransitionRecordSchema).default([]),
  // Response headers info
  cluster: z.string().optional(),
  requestId: z.string().optional(),
  eagleeyeTraceid: z.string().optional(),
});

export type SandboxStatusResponse = z.infer<typeof SandboxStatusResponseSchema>;

/**
 * Commit task phase
 */
export const CommitPhase = {
  RUNNING: 'RUNNING',
  SUCCEEDED: 'SUCCEEDED',
  FAILED: 'FAILED',
} as const;

export type CommitPhase = (typeof CommitPhase)[keyof typeof CommitPhase];

/**
 * Commit task status response
 */
export const CommitStatusResponseSchema = z.object({
  sandboxId: z.string(),
  imageTag: z.string(),
  phase: z.enum(['RUNNING', 'SUCCEEDED', 'FAILED']),
  startedAt: z.string(),
  completedAt: z.string().nullable().default(null),
  exitCode: z.number().nullable().default(null),
  failedStage: z.string().nullable().default(null),
  errorCode: z.string().nullable().default(null),
  errorMessage: z.string().nullable().default(null),
});

export type CommitStatusResponse = z.infer<typeof CommitStatusResponseSchema>;

/**
 * Command execution response
 */
export const CommandResponseSchema = z.object({
  stdout: z.string().default(''),
  stderr: z.string().default(''),
  exitCode: z.number().optional(),
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
  fileName: z.string().optional().default(''),
});

export type UploadResponse = z.infer<typeof UploadResponseSchema>;

/**
 * Bash observation (execution result)
 */
export const ObservationSchema = z.object({
  output: z.string().default(''),
  exitCode: z.number().optional(),
  failureReason: z.string().default(''),
  expectString: z.string().default(''),
});

export type Observation = z.infer<typeof ObservationSchema>;

/**
 * Create session response
 */
export const CreateSessionResponseSchema = z.object({
  output: z.string().default(''),
  sessionType: z.literal('bash').default('bash'),
});

export type CreateSessionResponse = z.infer<typeof CreateSessionResponseSchema>;

/**
 * Close session response
 */
export const CloseSessionResponseSchema = z.object({
  sessionType: z.literal('bash').default('bash'),
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

/**
 * Download file response
 */
export const DownloadFileResponseSchema = z.object({
  success: z.boolean().default(false),
  message: z.string().default(''),
});

export type DownloadFileResponse = z.infer<typeof DownloadFileResponseSchema>;

/**
 * OSS STS credentials from sandbox /get_token API
 * API returns snake_case, which gets converted to camelCase by HttpUtils
 */
export const OssCredentialsSchema = z.object({
  accessKeyId: z.string(),
  accessKeySecret: z.string(),
  securityToken: z.string(),
  expiration: z.string(),
  // Server-supplied OSS config (optional; only new admin returns these).
  // When present, takes priority over ROCK_OSS_BUCKET_* env vars so the bucket
  // matches the STS-issuing account.
  endpoint: z.string().optional(),
  bucket: z.string().optional(),
  region: z.string().optional(),
  prefix: z.string().optional(),
});

export type OssCredentials = z.infer<typeof OssCredentialsSchema>;
