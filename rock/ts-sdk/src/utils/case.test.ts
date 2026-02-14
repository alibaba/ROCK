/**
 * Case conversion utilities tests
 */

import { objectToCamel, objectToSnake } from './case.js';

describe('case conversion', () => {
  describe('objectToCamel', () => {
    test('converts snake_case keys to camelCase', () => {
      const input = {
        sandbox_id: '123',
        is_alive: true,
        host_name: 'localhost',
      };

      const result = objectToCamel(input);

      expect(result).toEqual({
        sandboxId: '123',
        isAlive: true,
        hostName: 'localhost',
      });
    });

    test('handles nested objects', () => {
      const input = {
        outer_key: {
          inner_key: 'value',
        },
      };

      const result = objectToCamel(input);

      expect(result).toEqual({
        outerKey: {
          innerKey: 'value',
        },
      });
    });

    test('handles arrays of objects', () => {
      const input = {
        items: [
          { item_id: 1, item_name: 'first' },
          { item_id: 2, item_name: 'second' },
        ],
      };

      const result = objectToCamel(input);

      expect(result).toEqual({
        items: [
          { itemId: 1, itemName: 'first' },
          { itemId: 2, itemName: 'second' },
        ],
      });
    });

    test('preserves primitive values', () => {
      const input = {
        string_val: 'hello',
        number_val: 42,
        bool_val: true,
        null_val: null,
      };

      const result = objectToCamel(input);

      expect(result).toEqual({
        stringVal: 'hello',
        numberVal: 42,
        boolVal: true,
        nullVal: null,
      });
    });
  });

  describe('objectToSnake', () => {
    test('converts camelCase keys to snake_case', () => {
      const input = {
        sandboxId: '123',
        isAlive: true,
        hostName: 'localhost',
      };

      const result = objectToSnake(input);

      expect(result).toEqual({
        sandbox_id: '123',
        is_alive: true,
        host_name: 'localhost',
      });
    });

    test('handles nested objects', () => {
      const input = {
        outerKey: {
          innerKey: 'value',
        },
      };

      const result = objectToSnake(input);

      expect(result).toEqual({
        outer_key: {
          inner_key: 'value',
        },
      });
    });

    test('handles arrays of objects', () => {
      const input = {
        items: [
          { itemId: 1, itemName: 'first' },
          { itemId: 2, itemName: 'second' },
        ],
      };

      const result = objectToSnake(input);

      expect(result).toEqual({
        items: [
          { item_id: 1, item_name: 'first' },
          { item_id: 2, item_name: 'second' },
        ],
      });
    });
  });
});
