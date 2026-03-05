/**
 * Deprecated decorator tests
 */

import { deprecated, deprecatedClass, clearDeprecatedWarnings } from './deprecated.js';
import { initLogger, clearLoggerCache } from '../logger.js';

describe('deprecated', () => {
  let warnSpy: jest.SpyInstance;
  let consoleWarnSpy: jest.SpyInstance;

  beforeEach(() => {
    clearLoggerCache();
    clearDeprecatedWarnings();
    // Create a spy on the logger's warn method
    const logger = initLogger('rock.deprecated');
    warnSpy = jest.spyOn(logger, 'warn').mockImplementation();
    // Spy on console.warn to verify it's NOT used
    consoleWarnSpy = jest.spyOn(console, 'warn').mockImplementation();
  });

  afterEach(() => {
    warnSpy.mockRestore();
    consoleWarnSpy.mockRestore();
  });

  describe('warn-once behavior', () => {
    test('warns only once when deprecated method is called multiple times', () => {
      class TestClass {
        @deprecated('Use newMethod instead')
        oldMethod(): string {
          return 'result';
        }
      }

      const instance = new TestClass();

      // Call the method 3 times
      instance.oldMethod();
      instance.oldMethod();
      instance.oldMethod();

      // Should only warn once
      expect(warnSpy).toHaveBeenCalledTimes(1);
      expect(warnSpy).toHaveBeenCalledWith('oldMethod is deprecated. Use newMethod instead');
    });

    test('different deprecated methods each warn once', () => {
      class TestClass {
        @deprecated('Use method1 instead')
        oldMethod1(): string {
          return 'result1';
        }

        @deprecated('Use method2 instead')
        oldMethod2(): string {
          return 'result2';
        }
      }

      const instance = new TestClass();

      instance.oldMethod1();
      instance.oldMethod1();
      instance.oldMethod2();
      instance.oldMethod2();

      // Each method should warn once
      expect(warnSpy).toHaveBeenCalledTimes(2);
      expect(warnSpy).toHaveBeenNthCalledWith(1, 'oldMethod1 is deprecated. Use method1 instead');
      expect(warnSpy).toHaveBeenNthCalledWith(2, 'oldMethod2 is deprecated. Use method2 instead');
    });

    test('different instances share the same warn-once state', () => {
      class TestClass {
        @deprecated('Use newMethod instead')
        oldMethod(): string {
          return 'result';
        }
      }

      const instance1 = new TestClass();
      const instance2 = new TestClass();

      instance1.oldMethod();
      instance2.oldMethod();

      // Should only warn once across instances
      expect(warnSpy).toHaveBeenCalledTimes(1);
    });
  });

  describe('Winston logger integration', () => {
    test('uses Winston logger instead of console.warn', () => {
      const consoleWarnSpy = jest.spyOn(console, 'warn').mockImplementation();

      class TestClass {
        @deprecated('This is deprecated')
        deprecatedMethod(): string {
          return 'result';
        }
      }

      const instance = new TestClass();
      instance.deprecatedMethod();

      // Should use Winston logger, not console.warn
      expect(warnSpy).toHaveBeenCalled();
      expect(consoleWarnSpy).not.toHaveBeenCalled();

      consoleWarnSpy.mockRestore();
    });
  });

  describe('functionality preservation', () => {
    test('deprecated method still returns correct value', () => {
      class TestClass {
        @deprecated('Use newMethod instead')
        oldMethod(value: number): number {
          return value * 2;
        }
      }

      const instance = new TestClass();
      expect(instance.oldMethod(5)).toBe(10);
    });

    test('deprecated method preserves this context', () => {
      class TestClass {
        private multiplier = 3;

        @deprecated('Use newMethod instead')
        oldMethod(value: number): number {
          return value * this.multiplier;
        }
      }

      const instance = new TestClass();
      expect(instance.oldMethod(4)).toBe(12);
    });

    test('deprecated method passes all arguments correctly', () => {
      class TestClass {
        @deprecated()
        sum(a: number, b: number, c: number): number {
          return a + b + c;
        }
      }

      const instance = new TestClass();
      expect(instance.sum(1, 2, 3)).toBe(6);
    });
  });

  describe('default reason', () => {
    test('works without providing a reason', () => {
      class TestClass {
        @deprecated()
        oldMethod(): string {
          return 'result';
        }
      }

      const instance = new TestClass();
      instance.oldMethod();

      expect(warnSpy).toHaveBeenCalledWith('oldMethod is deprecated. ');
    });
  });
});

describe('deprecatedClass', () => {
  let warnSpy: jest.SpyInstance;
  let consoleWarnSpy: jest.SpyInstance;

  beforeEach(() => {
    clearLoggerCache();
    clearDeprecatedWarnings();
    const logger = initLogger('rock.deprecated');
    warnSpy = jest.spyOn(logger, 'warn').mockImplementation();
    consoleWarnSpy = jest.spyOn(console, 'warn').mockImplementation();
  });

  afterEach(() => {
    warnSpy.mockRestore();
    consoleWarnSpy.mockRestore();
  });

  describe('warn-once behavior', () => {
    test('warns only once when deprecated class is instantiated multiple times', () => {
      @deprecatedClass('Use NewClass instead')
      class OldClass {
        value: string;
        constructor(value: string) {
          this.value = value;
        }
      }

      // Create 3 instances
      new OldClass('a');
      new OldClass('b');
      new OldClass('c');

      // Should only warn once
      expect(warnSpy).toHaveBeenCalledTimes(1);
      expect(warnSpy).toHaveBeenCalledWith('OldClass is deprecated. Use NewClass instead');
    });
  });

  describe('Winston logger integration', () => {
    test('uses Winston logger instead of console.warn', () => {
      const consoleWarnSpy = jest.spyOn(console, 'warn').mockImplementation();

      @deprecatedClass('This is deprecated')
      class DeprecatedClass {
        constructor() {}
      }

      new DeprecatedClass();

      expect(warnSpy).toHaveBeenCalled();
      expect(consoleWarnSpy).not.toHaveBeenCalled();

      consoleWarnSpy.mockRestore();
    });
  });

  describe('functionality preservation', () => {
    test('deprecated class constructor still works correctly', () => {
      @deprecatedClass('Use NewClass instead')
      class OldClass {
        value: number;
        constructor(value: number) {
          this.value = value * 2;
        }
      }

      const instance = new OldClass(5);
      expect(instance.value).toBe(10);
    });
  });
});
