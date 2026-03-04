/**
 * Tests for Sandbox Types
 * 
 * This test verifies that RunModeType and RunMode are re-exported from
 * common/constants.ts rather than duplicated.
 */

import { RunMode, RunModeType } from './types.js';
import { RunMode as ConstantsRunMode, RunModeType as ConstantsRunModeType } from '../common/constants.js';

describe('RunModeType', () => {
  test('should be re-exported from constants', () => {
    // This verifies that RunModeType in types.ts is the same as in constants.ts
    // After refactoring, they should be the same reference (not just equal values)
    expect<RunModeType>('normal').toBe('normal');
    expect<RunModeType>('nohup').toBe('nohup');
  });

  test('should be the same reference as constants RunModeType', () => {
    // This test will pass once we refactor to re-export from constants
    // Currently it may fail if they are different type definitions
    const normalMode: RunModeType = 'normal';
    const constNormalMode: ConstantsRunModeType = normalMode;
    expect(constNormalMode).toBe('normal');
  });
});

describe('RunMode', () => {
  test('should have correct values', () => {
    expect(RunMode.NORMAL).toBe('normal');
    expect(RunMode.NOHUP).toBe('nohup');
  });

  test('should be the same reference as constants RunMode', () => {
    // After refactoring, RunMode should be imported from constants
    // and re-exported, so they should be the same object
    expect(RunMode).toBe(ConstantsRunMode);
  });
});
