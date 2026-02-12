/**
 * Tests for Constants
 */

import { Constants, RunMode, PID_PREFIX, PID_SUFFIX } from './constants.js';

describe('Constants', () => {
  test('should have default values', () => {
    expect(Constants.BASE_URL_PRODUCT).toBe('');
    expect(Constants.BASE_URL_ALIYUN).toBe('');
    expect(Constants.REQUEST_TIMEOUT_SECONDS).toBe(180);
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
