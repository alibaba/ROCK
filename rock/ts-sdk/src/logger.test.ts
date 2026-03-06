/**
 * Logger module tests
 */

import { initLogger, clearLoggerCache, getLoggerCacheSize } from './logger.js';

describe('Logger', () => {
  beforeEach(() => {
    // Clear cache before each test
    clearLoggerCache();
  });

  describe('initLogger caching', () => {
    test('returns same logger instance for same name', () => {
      const logger1 = initLogger('test-logger');
      const logger2 = initLogger('test-logger');

      expect(logger1).toBe(logger2);
    });

    test('returns different logger instances for different names', () => {
      const logger1 = initLogger('logger-one');
      const logger2 = initLogger('logger-two');

      expect(logger1).not.toBe(logger2);
    });

    test('cache size increases when new logger names are used', () => {
      expect(getLoggerCacheSize()).toBe(0);

      initLogger('logger-a');
      expect(getLoggerCacheSize()).toBe(1);

      initLogger('logger-b');
      expect(getLoggerCacheSize()).toBe(2);
    });

    test('cache size does not increase when same name is used', () => {
      initLogger('same-logger');
      expect(getLoggerCacheSize()).toBe(1);

      initLogger('same-logger');
      expect(getLoggerCacheSize()).toBe(1);

      initLogger('same-logger');
      expect(getLoggerCacheSize()).toBe(1);
    });
  });

  describe('clearLoggerCache', () => {
    test('clears all cached loggers', () => {
      initLogger('logger-1');
      initLogger('logger-2');
      initLogger('logger-3');

      expect(getLoggerCacheSize()).toBe(3);

      clearLoggerCache();

      expect(getLoggerCacheSize()).toBe(0);
    });

    test('allows creating fresh logger after clear', () => {
      const logger1 = initLogger('fresh-logger');
      clearLoggerCache();
      const logger2 = initLogger('fresh-logger');

      // After clear, should be a new instance
      expect(logger1).not.toBe(logger2);
    });
  });
});
