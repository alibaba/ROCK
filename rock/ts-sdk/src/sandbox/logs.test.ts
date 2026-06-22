/**
 * Sandbox Logs unit tests
 */

import {
  validateLogPath,
  getLogBasePath,
  parseFileList,
  buildListCommand,
  matchPattern,
} from './logs.js';

describe('validateLogPath', () => {
  describe('absolute path rejection', () => {
    test('should reject path starting with /', () => {
      expect(() => validateLogPath('/etc/passwd')).toThrow('logPath must be relative path');
    });

    test('should reject root path', () => {
      expect(() => validateLogPath('/')).toThrow('logPath must be relative path');
    });

    test('should reject path with leading slash', () => {
      expect(() => validateLogPath('/data/logs/app.log')).toThrow('logPath must be relative path');
    });
  });

  describe('directory traversal rejection', () => {
    test('should reject path with ..', () => {
      expect(() => validateLogPath('../etc/passwd')).toThrow("logPath cannot contain '..'");
    });

    test('should reject path with embedded ..', () => {
      expect(() => validateLogPath('subdir/../../../etc/passwd')).toThrow("logPath cannot contain '..'");
    });

    test('should reject just ..', () => {
      expect(() => validateLogPath('..')).toThrow("logPath cannot contain '..'");
    });

    test('should reject .. at end', () => {
      expect(() => validateLogPath('subdir/..')).toThrow("logPath cannot contain '..'");
    });
  });

  describe('valid paths', () => {
    test('should accept simple filename', () => {
      expect(() => validateLogPath('app.log')).not.toThrow();
    });

    test('should accept filename with subdirectory', () => {
      expect(() => validateLogPath('subdir/app.log')).not.toThrow();
    });

    test('should accept nested subdirectory path', () => {
      expect(() => validateLogPath('a/b/c/app.log')).not.toThrow();
    });

    test('should accept path with dots in filename', () => {
      expect(() => validateLogPath('app.2024.01.01.log')).not.toThrow();
    });

    test('should accept path with single dot', () => {
      expect(() => validateLogPath('./app.log')).not.toThrow();
    });
  });
});

describe('getLogBasePath', () => {
  test('should return /data/logs for alive sandbox', () => {
    expect(getLogBasePath('sandbox-123', true)).toBe('/data/logs');
  });

  test('should return /data/logs/{sandboxId} for destroyed sandbox', () => {
    expect(getLogBasePath('sandbox-123', false)).toBe('/data/logs/sandbox-123');
  });

  test('should handle different sandbox IDs', () => {
    expect(getLogBasePath('abc-def-123', false)).toBe('/data/logs/abc-def-123');
    expect(getLogBasePath('test_sandbox', false)).toBe('/data/logs/test_sandbox');
  });
});

describe('parseFileList', () => {
  test('should parse find output with tab-separated values', () => {
    const output = `/data/logs/app.log\t1024\t1711900000.000
/data/logs/error.log\t512\t1711900100.000`;
    
    const result = parseFileList(output, '/data/logs');
    
    expect(result).toHaveLength(2);
    expect(result[0]).toEqual({
      name: 'app.log',
      path: 'app.log',
      size: 1024,
      modifiedTime: expect.any(String),
      isDirectory: false,
    });
    expect(result[1]).toEqual({
      name: 'error.log',
      path: 'error.log',
      size: 512,
      modifiedTime: expect.any(String),
      isDirectory: false,
    });
  });

  test('should handle nested paths', () => {
    const output = `/data/logs/subdir/app.log\t1024\t1711900000.000`;
    
    const result = parseFileList(output, '/data/logs');
    
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({
      name: 'app.log',
      path: 'subdir/app.log',
      size: 1024,
      modifiedTime: expect.any(String),
      isDirectory: false,
    });
  });

  test('should handle empty output', () => {
    const result = parseFileList('', '/data/logs');
    expect(result).toHaveLength(0);
  });

  test('should handle invalid lines', () => {
    const output = `invalid line
/data/logs/app.log\t1024\t1711900000.000`;
    
    const result = parseFileList(output, '/data/logs');
    
    expect(result).toHaveLength(1);
  });
});

describe('buildListCommand', () => {
  test('should build basic find command', () => {
    const cmd = buildListCommand('/data/logs');
    
    expect(cmd).toContain("find '/data/logs'");
    expect(cmd).toContain('-type f');
    expect(cmd).toContain("-printf '%p\\t%s\\t%T@\\n'");
  });

  test('should add -maxdepth 1 when recursive is false', () => {
    const cmd = buildListCommand('/data/logs', { recursive: false });
    
    expect(cmd).toContain('-maxdepth 1');
  });

  test('should not add -maxdepth when recursive is true', () => {
    const cmd = buildListCommand('/data/logs', { recursive: true });
    
    expect(cmd).not.toContain('-maxdepth');
  });

  test('should add -name filter with pattern', () => {
    const cmd = buildListCommand('/data/logs', { pattern: '*.log' });
    
    expect(cmd).toContain("-name '*.log'");
  });

  test('should combine options', () => {
    const cmd = buildListCommand('/data/logs', { recursive: false, pattern: '*.txt' });
    
    expect(cmd).toContain('-maxdepth 1');
    expect(cmd).toContain("-name '*.txt'");
  });
});

describe('matchPattern', () => {
  test('should match wildcard pattern *.log', () => {
    expect(matchPattern('app.log', '*.log')).toBe(true);
    expect(matchPattern('error.log', '*.log')).toBe(true);
    expect(matchPattern('app.txt', '*.log')).toBe(false);
  });

  test('should match exact filename', () => {
    expect(matchPattern('app.log', 'app.log')).toBe(true);
    expect(matchPattern('error.log', 'app.log')).toBe(false);
  });

  test('should match question mark wildcard', () => {
    expect(matchPattern('app1.log', 'app?.log')).toBe(true);
    expect(matchPattern('app2.log', 'app?.log')).toBe(true);
    expect(matchPattern('app.log', 'app?.log')).toBe(false);
  });

  test('should match complex patterns', () => {
    expect(matchPattern('app.2024.log', 'app.*.log')).toBe(true);
    expect(matchPattern('error.2024.01.01.log', '*.log')).toBe(true);
  });
});
