/**
 * ROCK Exception classes
 */

import { Codes } from '../types/codes.js';
import { SandboxResponse } from '../types/responses.js';

/**
 * Base ROCK exception
 */
export class RockException extends Error {
  protected _code: Codes | null = null;

  constructor(message: string, code?: Codes) {
    super(message);
    this.name = 'RockException';
    this._code = code ?? null;
  }

  get code(): Codes | null {
    return this._code;
  }
}

/**
 * Invalid parameter exception (deprecated)
 * @deprecated Use BadRequestRockError instead
 */
export class InvalidParameterRockException extends RockException {
  constructor(message: string) {
    super(message);
    this.name = 'InvalidParameterRockException';
  }
}

/**
 * Bad request error (4xxx)
 */
export class BadRequestRockError extends RockException {
  constructor(message: string, code: Codes = Codes.BAD_REQUEST) {
    super(message, code);
    this.name = 'BadRequestRockError';
  }
}

/**
 * Internal server error (5xxx)
 */
export class InternalServerRockError extends RockException {
  constructor(message: string, code: Codes = Codes.INTERNAL_SERVER_ERROR) {
    super(message, code);
    this.name = 'InternalServerRockError';
  }
}

/**
 * Command execution error (6xxx)
 */
export class CommandRockError extends RockException {
  constructor(message: string, code: Codes = Codes.COMMAND_ERROR) {
    super(message, code);
    this.name = 'CommandRockError';
  }
}

/**
 * Raise appropriate exception based on status code
 */
export function raiseForCode(code: Codes | null | undefined, message: string): void {
  if (code === null || code === undefined || isSuccessCode(code)) {
    return;
  }

  if (isClientErrorCode(code)) {
    throw new BadRequestRockError(message, code);
  }
  if (isServerErrorCode(code)) {
    throw new InternalServerRockError(message, code);
  }
  if (isCommandErrorCode(code)) {
    throw new CommandRockError(message, code);
  }

  throw new RockException(message, code);
}

/**
 * Convert RockException to SandboxResponse
 */
export function fromRockException(e: RockException): SandboxResponse {
  return {
    code: e.code ?? undefined,
    exitCode: 1,
    failureReason: e.message,
  };
}

// Helper functions for code checking
function isSuccessCode(code: Codes): boolean {
  return code >= 2000 && code <= 2999;
}

function isClientErrorCode(code: Codes): boolean {
  return code >= 4000 && code <= 4999;
}

function isServerErrorCode(code: Codes): boolean {
  return code >= 5000 && code <= 5999;
}

function isCommandErrorCode(code: Codes): boolean {
  return code >= 6000 && code <= 6999;
}
