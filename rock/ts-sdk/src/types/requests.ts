/**
 * Request types
 */

import { z } from 'zod';

/**
 * Command execution request
 */
export const CommandSchema = z.object({
  command: z.union([z.string(), z.array(z.string())]),
  timeout: z.number().optional().default(1200),
  env: z.record(z.string()).optional(),
  cwd: z.string().optional(),
});

export type Command = z.infer<typeof CommandSchema>;

/**
 * Bash session creation request
 */
export const CreateBashSessionRequestSchema = z.object({
  session: z.string().default('default'),
  startupSource: z.array(z.string()).default([]),
  envEnable: z.boolean().default(false),
  env: z.record(z.string()).optional(),
  remoteUser: z.string().optional(),
});

export type CreateBashSessionRequest = z.infer<typeof CreateBashSessionRequestSchema>;

/**
 * Bash action for session execution
 */
export const BashActionSchema = z.object({
  command: z.string(),
  session: z.string().default('default'),
  timeout: z.number().optional(),
  check: z.enum(['silent', 'raise', 'ignore']).default('raise'),
});

export type BashAction = z.infer<typeof BashActionSchema>;

/**
 * Write file request
 */
export const WriteFileRequestSchema = z.object({
  content: z.string(),
  path: z.string(),
});

export type WriteFileRequest = z.infer<typeof WriteFileRequestSchema>;

/**
 * Read file request
 */
export const ReadFileRequestSchema = z.object({
  path: z.string(),
  encoding: z.string().optional(),
  errors: z.string().optional(),
});

export type ReadFileRequest = z.infer<typeof ReadFileRequestSchema>;

/**
 * Upload file request
 */
export const UploadRequestSchema = z.object({
  sourcePath: z.string(),
  targetPath: z.string(),
});

export type UploadRequest = z.infer<typeof UploadRequestSchema>;

/**
 * Close session request
 */
export const CloseSessionRequestSchema = z.object({
  session: z.string().default('default'),
});

export type CloseSessionRequest = z.infer<typeof CloseSessionRequestSchema>;

/**
 * Chown request
 */
export const ChownRequestSchema = z.object({
  remoteUser: z.string(),
  paths: z.array(z.string()).default([]),
  recursive: z.boolean().default(false),
});

export type ChownRequest = z.infer<typeof ChownRequestSchema>;

/**
 * Chmod request
 */
export const ChmodRequestSchema = z.object({
  paths: z.array(z.string()).default([]),
  mode: z.string().default('755'),
  recursive: z.boolean().default(false),
});

export type ChmodRequest = z.infer<typeof ChmodRequestSchema>;
