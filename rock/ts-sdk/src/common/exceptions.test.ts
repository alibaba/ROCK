/**
 * Tests for Exceptions
 */

import {
  RockException,
  InvalidParameterRockException,
  BadRequestRockError,
  InternalServerRockError,
  CommandRockError,
  raiseForCode,
  raiseForEnvelopeOrResult,
  fromRockException,
} from './exceptions.js';
import { Codes } from '../types/codes.js';

describe('RockException', () => {
  test('should create exception with message', () => {
    const error = new RockException('Test error');
    expect(error.message).toBe('Test error');
    expect(error.name).toBe('RockException');
    expect(error.code).toBeNull();
  });

  test('should create exception with code', () => {
    const error = new RockException('Test error', Codes.BAD_REQUEST);
    expect(error.code).toBe(Codes.BAD_REQUEST);
  });
});

describe('InvalidParameterRockException', () => {
  test('should create deprecated exception', () => {
    const error = new InvalidParameterRockException('Invalid param');
    expect(error.message).toBe('Invalid param');
    expect(error.name).toBe('InvalidParameterRockException');
  });
});

describe('BadRequestRockError', () => {
  test('should create with default code', () => {
    const error = new BadRequestRockError('Bad request');
    expect(error.message).toBe('Bad request');
    expect(error.name).toBe('BadRequestRockError');
    expect(error.code).toBe(Codes.BAD_REQUEST);
  });

  test('should create with custom code', () => {
    const error = new BadRequestRockError('Bad request', 4001 as Codes);
    expect(error.code).toBe(4001);
  });
});

describe('InternalServerRockError', () => {
  test('should create with default code', () => {
    const error = new InternalServerRockError('Server error');
    expect(error.message).toBe('Server error');
    expect(error.name).toBe('InternalServerRockError');
    expect(error.code).toBe(Codes.INTERNAL_SERVER_ERROR);
  });
});

describe('CommandRockError', () => {
  test('should create with default code', () => {
    const error = new CommandRockError('Command failed');
    expect(error.message).toBe('Command failed');
    expect(error.name).toBe('CommandRockError');
    expect(error.code).toBe(Codes.COMMAND_ERROR);
  });
});

describe('raiseForCode', () => {
  test('should not throw for null code', () => {
    expect(() => raiseForCode(null, 'test')).not.toThrow();
  });

  test('should not throw for undefined code', () => {
    expect(() => raiseForCode(undefined, 'test')).not.toThrow();
  });

  test('should not throw for success code', () => {
    expect(() => raiseForCode(Codes.OK, 'test')).not.toThrow();
  });

  test('should throw BadRequestRockError for 4xxx code', () => {
    expect(() => raiseForCode(Codes.BAD_REQUEST, 'test')).toThrow(BadRequestRockError);
  });

  test('should throw InternalServerRockError for 5xxx code', () => {
    expect(() => raiseForCode(Codes.INTERNAL_SERVER_ERROR, 'test')).toThrow(InternalServerRockError);
  });

  test('should throw CommandRockError for 6xxx code', () => {
    expect(() => raiseForCode(Codes.COMMAND_ERROR, 'test')).toThrow(CommandRockError);
  });

  test('should throw RockException for unknown error code', () => {
    expect(() => raiseForCode(7000 as Codes, 'test')).toThrow(RockException);
  });
});

describe('raiseForEnvelopeOrResult', () => {
  test('should prefer envelope code over result code', () => {
    const response = {
      status: 'Failed',
      code: Codes.BAD_REQUEST,
      error: 'envelope error',
      result: { code: Codes.INTERNAL_SERVER_ERROR, failure_reason: 'stale' },
    };
    expect(() => raiseForEnvelopeOrResult(response, 'Failed to start')).toThrow(BadRequestRockError);
  });

  test('should fall back to legacy result.code with deprecation warning', () => {
    const warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});
    const response = {
      status: 'Failed',
      result: { code: Codes.INTERNAL_SERVER_ERROR, failure_reason: 'legacy' },
    };
    expect(() => raiseForEnvelopeOrResult(response, 'Failed to start')).toThrow(InternalServerRockError);
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('deprecated')
    );
    warnSpy.mockRestore();
  });

  test('should throw generic Error when neither envelope nor result code present', () => {
    const response = { status: 'Failed', error: 'anything' };
    expect(() => raiseForEnvelopeOrResult(response, 'Failed to start')).toThrow(Error);
    expect(() => raiseForEnvelopeOrResult(response, 'Failed to start')).not.toThrow(RockException);
  });

  test('should skip legacy path when result is not an object', () => {
    const response = { status: 'Failed', error: 'oops', result: 'some string' };
    expect(() => raiseForEnvelopeOrResult(response, 'Failed to start')).toThrow(Error);
    expect(() => raiseForEnvelopeOrResult(response, 'Failed to start')).not.toThrow(RockException);
  });
});

describe('fromRockException', () => {
  test('should convert exception to response', () => {
    const error = new BadRequestRockError('Test error');
    const response = fromRockException(error);

    expect(response.code).toBe(Codes.BAD_REQUEST);
    expect(response.exitCode).toBe(1);
    expect(response.failureReason).toBe('Test error');
  });

  test('should handle exception without code', () => {
    const error = new RockException('Test error');
    const response = fromRockException(error);

    expect(response.code).toBeUndefined();
    expect(response.exitCode).toBe(1);
    expect(response.failureReason).toBe('Test error');
  });
});
