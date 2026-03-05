/**
 * Tests for system utilities
 * 
 * These tests verify that the SDK explicitly requires Node.js environment
 * and does not include misleading browser compatibility checks.
 */

import { isNode, getEnv, getRequiredEnv, isEnvSet } from './system.js';

describe('system utilities', () => {
  describe('isNode', () => {
    test('returns true in Node.js environment', () => {
      expect(isNode()).toBe(true);
    });
  });

  describe('getEnv', () => {
    const originalEnv = process.env;

    beforeEach(() => {
      // Reset process.env for each test
      process.env = { ...originalEnv };
    });

    afterAll(() => {
      process.env = originalEnv;
    });

    test('returns environment variable value when set', () => {
      process.env.TEST_VAR = 'test-value';
      expect(getEnv('TEST_VAR')).toBe('test-value');
    });

    test('returns undefined when variable not set and no default', () => {
      delete process.env.NONEXISTENT_VAR;
      expect(getEnv('NONEXISTENT_VAR')).toBeUndefined();
    });

    test('returns default value when variable not set', () => {
      delete process.env.NONEXISTENT_VAR;
      expect(getEnv('NONEXISTENT_VAR', 'default-value')).toBe('default-value');
    });

    test('returns actual value even when default is provided', () => {
      process.env.TEST_VAR = 'actual-value';
      expect(getEnv('TEST_VAR', 'default-value')).toBe('actual-value');
    });
  });

  describe('getRequiredEnv', () => {
    const originalEnv = process.env;

    beforeEach(() => {
      process.env = { ...originalEnv };
    });

    afterAll(() => {
      process.env = originalEnv;
    });

    test('returns value when environment variable is set', () => {
      process.env.REQUIRED_VAR = 'required-value';
      expect(getRequiredEnv('REQUIRED_VAR')).toBe('required-value');
    });

    test('throws error when environment variable is not set', () => {
      delete process.env.NONEXISTENT_REQUIRED_VAR;
      expect(() => getRequiredEnv('NONEXISTENT_REQUIRED_VAR')).toThrow(
        'Required environment variable NONEXISTENT_REQUIRED_VAR is not set'
      );
    });
  });

  describe('isEnvSet', () => {
    const originalEnv = process.env;

    beforeEach(() => {
      process.env = { ...originalEnv };
    });

    afterAll(() => {
      process.env = originalEnv;
    });

    test('returns true when environment variable is set', () => {
      process.env.SET_VAR = 'value';
      expect(isEnvSet('SET_VAR')).toBe(true);
    });

    test('returns true when environment variable is set to empty string', () => {
      process.env.EMPTY_VAR = '';
      expect(isEnvSet('EMPTY_VAR')).toBe(true);
    });

    test('returns false when environment variable is not set', () => {
      delete process.env.UNSET_VAR;
      expect(isEnvSet('UNSET_VAR')).toBe(false);
    });
  });
});

/**
 * Test that isBrowser is NOT exported from the main index.
 * This SDK requires Node.js and should not have misleading browser checks.
 */
describe('Node.js only requirement', () => {
  test('isBrowser should not be exported from main index', async () => {
    const mainModule = await import('../index.js');
    
    // isBrowser should NOT be exported - this is intentional
    expect('isBrowser' in mainModule).toBe(false);
  });

  test('isBrowser should not be exported from system.js', async () => {
    // Re-import to check the module's exports
    const systemModule = await import('./system.js');
    
    // isBrowser should NOT be exported
    expect('isBrowser' in systemModule).toBe(false);
  });
});
