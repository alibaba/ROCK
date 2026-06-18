import { DEFAULT_WAIT_TIMEOUT, CHECK_INTERVAL, USER_DEFINED_LOGS } from './constants';

describe('bench constants', () => {
  test('DEFAULT_WAIT_TIMEOUT is 7200', () => {
    expect(DEFAULT_WAIT_TIMEOUT).toBe(7200);
  });

  test('CHECK_INTERVAL is 30', () => {
    expect(CHECK_INTERVAL).toBe(30);
  });

  test('USER_DEFINED_LOGS is the correct path', () => {
    expect(USER_DEFINED_LOGS).toBe('/data/logs/user-defined');
  });
});
