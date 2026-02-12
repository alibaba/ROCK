/**
 * ROCK status codes enumeration
 */

/**
 * Status codes with phrase descriptions
 */
export enum Codes {
  /**
   * Success codes (2xxx)
   */
  OK = 2000,

  /**
   * Client error codes (4xxx)
   */
  BAD_REQUEST = 4000,

  /**
   * Server error codes (5xxx)
   */
  INTERNAL_SERVER_ERROR = 5000,

  /**
   * Command/execution error codes (6xxx)
   */
  COMMAND_ERROR = 6000,
}

/**
 * Human-readable reason phrases for status codes
 */
export const ReasonPhrases: Record<Codes, string> = {
  [Codes.OK]: 'OK',
  [Codes.BAD_REQUEST]: 'Bad Request',
  [Codes.INTERNAL_SERVER_ERROR]: 'Internal Server Error',
  [Codes.COMMAND_ERROR]: 'Command Error',
};

/**
 * Get the reason phrase for a given status code
 */
export function getReasonPhrase(code: Codes): string {
  return ReasonPhrases[code] ?? '';
}

/**
 * Check if a status code indicates success (2xxx range)
 */
export function isSuccess(code: Codes): boolean {
  return code >= 2000 && code <= 2999;
}

/**
 * Check if a status code indicates a client error (4xxx range)
 */
export function isClientError(code: Codes): boolean {
  return code >= 4000 && code <= 4999;
}

/**
 * Check if a status code indicates a server error (5xxx range)
 */
export function isServerError(code: Codes): boolean {
  return code >= 5000 && code <= 5999;
}

/**
 * Check if a status code indicates a command error (6xxx range)
 */
export function isCommandError(code: Codes): boolean {
  return code >= 6000 && code <= 6999;
}

/**
 * Check if a status code indicates any kind of error
 */
export function isError(code: Codes): boolean {
  return code >= 4000 && code <= 6999;
}
