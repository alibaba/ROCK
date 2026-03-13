/**
 * Tests for shell utility functions
 * These tests verify command injection protection
 */

import {
  shellQuote,
  buildCommand,
  bashWrap,
  SafeString,
  validateUrl,
  validateIpAddress,
  validatePath,
  validateUsername,
  validateChmodMode,
} from './shell.js';

describe('shellQuote', () => {
  test('quotes simple string', () => {
    expect(shellQuote('hello')).toBe("'hello'");
  });

  test('quotes string with spaces', () => {
    expect(shellQuote('hello world')).toBe("'hello world'");
  });

  test('escapes single quotes in string', () => {
    // Single quotes in input should be escaped
    expect(shellQuote("it's")).toBe("'it'\\''s'");
  });

  test('quotes path with special characters', () => {
    expect(shellQuote('/tmp/my file.txt')).toBe("'/tmp/my file.txt'");
  });

  test('handles injection attempt with semicolon', () => {
    // rm -rf / should NOT be executed
    const malicious = '/tmp; rm -rf /';
    expect(shellQuote(malicious)).toBe("'/tmp; rm -rf /'");
  });

  test('handles injection attempt with backticks', () => {
    const malicious = '/tmp/`whoami`';
    expect(shellQuote(malicious)).toBe("'/tmp/`whoami`'");
  });

  test('handles injection attempt with $()', () => {
    const malicious = '/tmp/$(whoami)';
    expect(shellQuote(malicious)).toBe("'/tmp/$(whoami)'");
  });

  test('handles injection attempt with &&', () => {
    const malicious = '/tmp && cat /etc/passwd';
    expect(shellQuote(malicious)).toBe("'/tmp && cat /etc/passwd'");
  });

  test('handles injection attempt with ||', () => {
    const malicious = '/tmp || rm -rf /';
    expect(shellQuote(malicious)).toBe("'/tmp || rm -rf /'");
  });

  test('handles injection attempt with pipe', () => {
    const malicious = '/tmp | cat /etc/passwd';
    expect(shellQuote(malicious)).toBe("'/tmp | cat /etc/passwd'");
  });

  test('handles newline injection attempt', () => {
    const malicious = '/tmp\necho pwned';
    expect(shellQuote(malicious)).toBe("'/tmp\necho pwned'");
  });

  test('handles empty string', () => {
    expect(shellQuote('')).toBe("''");
  });
});

describe('buildCommand', () => {
  test('builds command from multiple parts', () => {
    const cmd = buildCommand(['echo', 'hello world']);
    // All parts are quoted for maximum safety
    expect(cmd).toBe("'echo' 'hello world'");
  });

  test('quotes all string parts', () => {
    const cmd = buildCommand(['rm', '-rf', '/tmp/my dir']);
    expect(cmd).toBe("'rm' '-rf' '/tmp/my dir'");
  });

  test('does not quote SafeString parts', () => {
    const cmd = buildCommand([new SafeString('echo'), new SafeString('hello')]);
    expect(cmd).toBe('echo hello');
  });

  test('handles injection attempts in parts', () => {
    const cmd = buildCommand(['echo', '/tmp; rm -rf /']);
    // The malicious part is quoted, neutralizing the injection
    expect(cmd).toBe("'echo' '/tmp; rm -rf /'");
  });

  test('mixed SafeString and regular strings', () => {
    const cmd = buildCommand([new SafeString('echo'), 'hello world']);
    expect(cmd).toBe("echo 'hello world'");
  });
});

describe('bashWrap', () => {
  test('wraps command with bash -c', () => {
    const result = bashWrap('echo hello');
    expect(result).toBe("bash -c 'echo hello'");
  });

  test('properly escapes complex command', () => {
    const result = bashWrap('rm -rf /tmp/my dir');
    expect(result).toBe("bash -c 'rm -rf /tmp/my dir'");
  });

  test('handles injection attempts', () => {
    const result = bashWrap('echo test; rm -rf /');
    expect(result).toBe("bash -c 'echo test; rm -rf /'");
    // The entire string is quoted, so the injection is neutralized
  });
});

describe('validateUrl', () => {
  test('accepts valid http URL', () => {
    expect(validateUrl('http://example.com')).toBe('http://example.com');
  });

  test('accepts valid https URL', () => {
    expect(validateUrl('https://example.com')).toBe('https://example.com');
  });

  test('removes trailing slash', () => {
    expect(validateUrl('https://example.com/')).toBe('https://example.com');
  });

  test('rejects ftp URL', () => {
    expect(() => validateUrl('ftp://example.com')).toThrow('Protocol ftp:');
  });

  test('rejects invalid URL', () => {
    expect(() => validateUrl('not a url')).toThrow('Invalid URL');
  });

  test('rejects javascript URL', () => {
    expect(() => validateUrl('javascript:alert(1)')).toThrow('Protocol javascript:');
  });

  test('rejects file URL', () => {
    expect(() => validateUrl('file:///etc/passwd')).toThrow('Protocol file:');
  });

  test('accepts URL with path', () => {
    expect(validateUrl('https://example.com/path/to/resource')).toBe(
      'https://example.com/path/to/resource'
    );
  });

  test('accepts URL with port', () => {
    expect(validateUrl('https://example.com:8080')).toBe('https://example.com:8080');
  });
});

describe('validateIpAddress', () => {
  test('accepts valid IP address', () => {
    expect(validateIpAddress('192.168.1.1')).toBe('192.168.1.1');
  });

  test('accepts 0.0.0.0', () => {
    expect(validateIpAddress('0.0.0.0')).toBe('0.0.0.0');
  });

  test('accepts 255.255.255.255', () => {
    expect(validateIpAddress('255.255.255.255')).toBe('255.255.255.255');
  });

  test('rejects IP with octet > 255', () => {
    expect(() => validateIpAddress('192.168.1.256')).toThrow('Invalid IP address');
  });

  test('rejects IP with missing octet', () => {
    expect(() => validateIpAddress('192.168.1')).toThrow('Invalid IP address format');
  });

  test('rejects IP with extra octet', () => {
    expect(() => validateIpAddress('192.168.1.1.1')).toThrow('Invalid IP address format');
  });

  test('rejects non-numeric IP', () => {
    expect(() => validateIpAddress('192.168.1.abc')).toThrow('Invalid IP address format');
  });

  test('rejects IP with injection attempt', () => {
    expect(() => validateIpAddress('192.168.1.1; rm -rf /')).toThrow('Invalid IP address format');
  });

  test('rejects empty string', () => {
    expect(() => validateIpAddress('')).toThrow('Invalid IP address format');
  });
});

describe('validatePath', () => {
  test('accepts valid absolute path', () => {
    expect(() => validatePath('/tmp/file.txt')).not.toThrow();
  });

  test('accepts path with subdirectories', () => {
    expect(() => validatePath('/tmp/dir/file.txt')).not.toThrow();
  });

  test('rejects relative path', () => {
    expect(() => validatePath('tmp/file.txt')).toThrow('must be absolute');
  });

  test('rejects path traversal with ..', () => {
    expect(() => validatePath('/tmp/../etc/passwd')).toThrow('cannot contain ..');
  });

  test('rejects path with backticks', () => {
    expect(() => validatePath('/tmp/`whoami`')).toThrow('forbidden characters');
  });

  test('rejects path with $()', () => {
    expect(() => validatePath('/tmp/$(whoami)')).toThrow('forbidden characters');
  });

  test('rejects path with semicolon', () => {
    expect(() => validatePath('/tmp; rm -rf /')).toThrow('forbidden characters');
  });

  test('rejects path with pipe', () => {
    expect(() => validatePath('/tmp | cat')).toThrow('forbidden characters');
  });

  test('rejects path with &&', () => {
    expect(() => validatePath('/tmp && echo pwned')).toThrow('forbidden characters');
  });
});

describe('SafeString', () => {
  test('creates SafeString with value', () => {
    const safe = new SafeString('trusted');
    expect(safe.value).toBe('trusted');
  });

  test('SafeString instance check works', () => {
    const safe = new SafeString('trusted');
    expect(safe instanceof SafeString).toBe(true);
  });
});

describe('validateUsername', () => {
  test('accepts valid username', () => {
    expect(() => validateUsername('root')).not.toThrow();
  });

  test('accepts username with underscore', () => {
    expect(() => validateUsername('valid_user')).not.toThrow();
  });

  test('accepts username with hyphen', () => {
    expect(() => validateUsername('valid-user')).not.toThrow();
  });

  test('accepts username starting with letter', () => {
    expect(() => validateUsername('user123')).not.toThrow();
  });

  test('accepts username starting with underscore', () => {
    expect(() => validateUsername('_system')).not.toThrow();
  });

  test('rejects username starting with dash', () => {
    expect(() => validateUsername('-rf')).toThrow('not allowed');
  });

  test('rejects username starting with digit', () => {
    expect(() => validateUsername('123user')).toThrow('Invalid username');
  });

  test('rejects username with semicolon', () => {
    expect(() => validateUsername('user;rm')).toThrow('Invalid username');
  });

  test('rejects username with space', () => {
    expect(() => validateUsername('user name')).toThrow('Invalid username');
  });

  test('rejects empty username', () => {
    expect(() => validateUsername('')).toThrow('cannot be empty');
  });
});

describe('validateChmodMode', () => {
  test('accepts valid octal mode (3 digits)', () => {
    expect(() => validateChmodMode('755')).not.toThrow();
  });

  test('accepts valid octal mode (4 digits)', () => {
    expect(() => validateChmodMode('0755')).not.toThrow();
  });

  test('accepts valid symbolic mode', () => {
    expect(() => validateChmodMode('u+x')).not.toThrow();
  });

  test('accepts symbolic mode with multiple clauses', () => {
    expect(() => validateChmodMode('u+x,go-w')).not.toThrow();
  });

  test('accepts symbolic mode with a (all)', () => {
    expect(() => validateChmodMode('a+r')).not.toThrow();
  });

  test('rejects mode with semicolon', () => {
    expect(() => validateChmodMode('755; rm -rf /')).toThrow('Invalid chmod mode');
  });

  test('rejects mode with invalid octal digit', () => {
    expect(() => validateChmodMode('789')).toThrow('Invalid chmod mode');
  });

  test('rejects non-octal letters', () => {
    expect(() => validateChmodMode('abc')).toThrow('Invalid chmod mode');
  });

  test('rejects empty mode', () => {
    expect(() => validateChmodMode('')).toThrow('cannot be empty');
  });
});
