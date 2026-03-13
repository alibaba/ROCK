/**
 * Tests for Sandbox Utils
 */

import { extractNohupPid } from './utils.js';
import { extractNohupPid as httpExtractNohupPid } from '../utils/http.js';
import { PID_PREFIX, PID_SUFFIX } from '../common/constants.js';

describe('extractNohupPid', () => {
  test('should be the same function as in utils/http.ts', () => {
    // After refactoring, extractNohupPid should be imported from utils/http.ts
    // and re-exported, so they should be the same function reference
    expect(extractNohupPid).toBe(httpExtractNohupPid);
  });

  test('should extract PID from valid output', () => {
    const output = `some output\n${PID_PREFIX}12345${PID_SUFFIX}\nmore output`;
    expect(extractNohupPid(output)).toBe(12345);
  });

  test('should return null for invalid output', () => {
    expect(extractNohupPid('no pid here')).toBeNull();
  });

  test('should return null for empty output', () => {
    expect(extractNohupPid('')).toBeNull();
  });

  test('should handle PID at start of output', () => {
    const output = `${PID_PREFIX}99999${PID_SUFFIX}`;
    expect(extractNohupPid(output)).toBe(99999);
  });
});
