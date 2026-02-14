/**
 * Deprecated decorator utilities
 */

/**
 * Mark a function as deprecated
 */
export function deprecated(reason: string = ''): MethodDecorator {
  return function (
    target: unknown,
    propertyKey: string | symbol,
    descriptor: PropertyDescriptor
  ): PropertyDescriptor {
    const originalMethod = descriptor.value;

    descriptor.value = function (...args: unknown[]): unknown {
      console.warn(
        `${String(propertyKey)} is deprecated. ${reason}`
      );
      return originalMethod.apply(this, args);
    };

    return descriptor;
  };
}

/**
 * Mark a class as deprecated
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function deprecatedClass(reason: string = ''): <T extends new (...args: any[]) => any>(constructor: T) => T {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return function <T extends new (...args: any[]) => any>(
    constructor: T
  ): T {
    return class extends constructor {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      constructor(...args: any[]) {
        console.warn(`${constructor.name} is deprecated. ${reason}`);
        super(...args);
      }
    };
  };
}
