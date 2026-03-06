/**
 * Case conversion utilities
 * Wraps ts-case-convert for consistent API
 */

import {
  objectToCamel as toCamel,
  objectToSnake as toSnake,
} from 'ts-case-convert';

/**
 * Convert object keys from snake_case to camelCase
 */
export const objectToCamel = toCamel;

/**
 * Convert object keys from camelCase to snake_case
 */
export const objectToSnake = toSnake;
