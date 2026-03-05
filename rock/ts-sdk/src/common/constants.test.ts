/**
 * Tests for Constants
 */

import * as constantsModule from './constants.js';
import { RunMode, PID_PREFIX, PID_SUFFIX } from './constants.js';

describe('Constants class removal', () => {
  test('Constants class should NOT be exported (dead code removal)', () => {
    // Constants class was deprecated with empty string values and never used
    // It should be removed from the codebase
    expect('Constants' in constantsModule).toBe(false);
  });
});

describe('RunMode', () => {
  test('should have correct values', () => {
    expect(RunMode.NORMAL).toBe('normal');
    expect(RunMode.NOHUP).toBe('nohup');
  });
});

describe('PID markers', () => {
  test('should have correct prefix and suffix', () => {
    expect(PID_PREFIX).toBe('__ROCK_PID_START__');
    expect(PID_SUFFIX).toBe('__ROCK_PID_END__');
  });
});
