/**
 * Deprecated decorator utilities
 */

/**
 * Mark a function as deprecated
 */
export function deprecated(reason: string = '') {
  return function (
    target: unknown,
    propertyKey: string,
    descriptor: PropertyDescriptor
  ): PropertyDescriptor {
    const originalMethod = descriptor.value;

    descriptor.value = function (...args: unknown[]) {
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
export function deprecatedClass(reason: string = '') {
  return function <T extends new (...args: any[]) => any>(
    constructor: T
  ): T {
    return class extends constructor {
      constructor(...args: any[]) {
        console.warn(`${constructor.name} is deprecated. ${reason}`);
        super(...args);
      }
    };
  };
}
