/**
 * Tests for Codes and code utilities
 */

import {
  Codes,
  ReasonPhrases,
  getReasonPhrase,
  isSuccess,
  isClientError,
  isServerError,
  isCommandError,
  isError,
} from './codes.js';

describe('Codes', () => {
  test('should have correct values', () => {
    expect(Codes.OK).toBe(2000);
    expect(Codes.BAD_REQUEST).toBe(4000);
    expect(Codes.INTERNAL_SERVER_ERROR).toBe(5000);
    expect(Codes.COMMAND_ERROR).toBe(6000);
  });

  test('should have correct reason phrases', () => {
    expect(ReasonPhrases[Codes.OK]).toBe('OK');
    expect(ReasonPhrases[Codes.BAD_REQUEST]).toBe('Bad Request');
    expect(ReasonPhrases[Codes.INTERNAL_SERVER_ERROR]).toBe('Internal Server Error');
    expect(ReasonPhrases[Codes.COMMAND_ERROR]).toBe('Command Error');
  });
});

describe('getReasonPhrase', () => {
  test('should return correct phrase for valid codes', () => {
    expect(getReasonPhrase(Codes.OK)).toBe('OK');
    expect(getReasonPhrase(Codes.BAD_REQUEST)).toBe('Bad Request');
  });

  test('should return empty string for invalid codes', () => {
    expect(getReasonPhrase(9999 as Codes)).toBe('');
  });
});

describe('isSuccess', () => {
  test('should return true for 2xxx codes', () => {
    expect(isSuccess(Codes.OK)).toBe(true);
    expect(isSuccess(2001 as Codes)).toBe(true);
    expect(isSuccess(2999 as Codes)).toBe(true);
  });

  test('should return false for non-2xxx codes', () => {
    expect(isSuccess(Codes.BAD_REQUEST)).toBe(false);
    expect(isSuccess(Codes.INTERNAL_SERVER_ERROR)).toBe(false);
  });
});

describe('isClientError', () => {
  test('should return true for 4xxx codes', () => {
    expect(isClientError(Codes.BAD_REQUEST)).toBe(true);
    expect(isClientError(4001 as Codes)).toBe(true);
    expect(isClientError(4999 as Codes)).toBe(true);
  });

  test('should return false for non-4xxx codes', () => {
    expect(isClientError(Codes.OK)).toBe(false);
    expect(isClientError(Codes.INTERNAL_SERVER_ERROR)).toBe(false);
  });
});

describe('isServerError', () => {
  test('should return true for 5xxx codes', () => {
    expect(isServerError(Codes.INTERNAL_SERVER_ERROR)).toBe(true);
    expect(isServerError(5001 as Codes)).toBe(true);
    expect(isServerError(5999 as Codes)).toBe(true);
  });

  test('should return false for non-5xxx codes', () => {
    expect(isServerError(Codes.OK)).toBe(false);
    expect(isServerError(Codes.BAD_REQUEST)).toBe(false);
  });
});

describe('isCommandError', () => {
  test('should return true for 6xxx codes', () => {
    expect(isCommandError(Codes.COMMAND_ERROR)).toBe(true);
    expect(isCommandError(6001 as Codes)).toBe(true);
    expect(isCommandError(6999 as Codes)).toBe(true);
  });

  test('should return false for non-6xxx codes', () => {
    expect(isCommandError(Codes.OK)).toBe(false);
    expect(isCommandError(Codes.BAD_REQUEST)).toBe(false);
  });
});

describe('isError', () => {
  test('should return true for error codes (4xxx-6xxx)', () => {
    expect(isError(Codes.BAD_REQUEST)).toBe(true);
    expect(isError(Codes.INTERNAL_SERVER_ERROR)).toBe(true);
    expect(isError(Codes.COMMAND_ERROR)).toBe(true);
  });

  test('should return false for success codes', () => {
    expect(isError(Codes.OK)).toBe(false);
  });
});
