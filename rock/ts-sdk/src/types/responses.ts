/**
 * Response types
 */

import { z } from 'zod';
import { Codes } from './codes.js';

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
});

export type SandboxStatusResponse = z.infer<typeof SandboxStatusResponseSchema>;

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
  fileName: z.string().optional(),
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
});

export type CreateSessionResponse = z.infer<typeof CreateSessionResponseSchema>;

/**
 * Close session response
 */
export const CloseSessionResponseSchema = z.object({});

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
