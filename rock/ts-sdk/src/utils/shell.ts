/**
 * Shell utility functions for safe command construction
 * Provides protection against command injection attacks
 *
 * Inspired by Python's shlex module and security best practices
 */

/**
 * Safely escape a string for use in shell commands
 * This is the TypeScript equivalent of Python's shlex.quote()
 *
 * The algorithm wraps the string in single quotes and escapes any
 * existing single quotes by ending the quote, adding an escaped quote,
 * and starting a new quote.
 *
 * @param str - The string to escape
 * @returns The escaped string, safe for shell use
 *
 * @example
 * ```typescript
 * shellQuote('hello');        // Returns: 'hello'
 * shellQuote("it's");         // Returns: 'it'\''s'
 * shellQuote('/tmp; rm -rf /'); // Returns: '/tmp; rm -rf /'
 * ```
 */
export function shellQuote(str: string): string {
  // Empty string
  if (str === '') {
    return "''";
  }

  // If string only contains safe characters (alphanumeric, underscore, hyphen, dot, slash)
  // we could potentially skip quoting, but for maximum safety we always quote.
  // This follows the principle of "defense in depth".

  // Wrap in single quotes and escape any single quotes within
  // The pattern: 'str' becomes 'str'\''str' for each embedded quote
  return `'${str.replace(/'/g, "'\\''")}'`;
}

/**
 * Mark a string as safe (not to be escaped)
 * Use with caution - only for trusted, pre-validated strings
 */
export class SafeString {
  constructor(public readonly value: string) {}
}

/**
 * Build a safe command from parts
 * Strings are automatically quoted, SafeString instances are passed through
 *
 * @param parts - Array of command parts (strings or SafeString)
 * @returns The constructed command string
 *
 * @example
 * ```typescript
 * const cmd = buildCommand(['echo', 'hello world']);
 * // Returns: "echo 'hello world'"
 *
 * const cmd2 = buildCommand(['rm', '-rf', new SafeString('/tmp/*')]);
 * // Returns: "rm -rf /tmp/*"
 * ```
 */
export function buildCommand(parts: Array<string | SafeString>): string {
  return parts.map((p) => (p instanceof SafeString ? p.value : shellQuote(p))).join(' ');
}

/**
 * Wrap a command with bash -c safely
 * The entire command string is quoted to prevent injection
 *
 * @param cmd - The command to wrap
 * @returns The wrapped command string
 *
 * @example
 * ```typescript
 * const wrapped = bashWrap('echo "hello world"');
 * // Returns: "bash -c 'echo \"hello world\"'"
 * ```
 */
export function bashWrap(cmd: string): string {
  return `bash -c ${shellQuote(cmd)}`;
}

/**
 * Validate and normalize a URL
 * Returns the normalized URL or throws an error
 *
 * @param url - The URL to validate
 * @param allowedProtocols - Array of allowed protocols (default: ['http:', 'https:'])
 * @returns The normalized URL (trailing slash removed)
 * @throws Error if URL is invalid or uses disallowed protocol
 *
 * @example
 * ```typescript
 * validateUrl('https://example.com/'); // Returns 'https://example.com'
 * validateUrl('ftp://example.com');    // Throws error
 * ```
 */
export function validateUrl(url: string, allowedProtocols = ['http:', 'https:']): string {
  try {
    const parsed = new URL(url);
    if (!allowedProtocols.includes(parsed.protocol)) {
      throw new Error(
        `Protocol ${parsed.protocol} is not allowed. Allowed: ${allowedProtocols.join(', ')}`
      );
    }
    // Remove trailing slash for consistency
    return url.replace(/\/$/, '');
  } catch (e) {
    if (e instanceof Error && e.message.includes('Protocol')) {
      throw e;
    }
    throw new Error(`Invalid URL: ${url}. ${e instanceof Error ? e.message : String(e)}`);
  }
}

/**
 * Validate an IPv4 address
 * Returns the validated IP or throws an error
 *
 * @param ip - The IP address to validate
 * @returns The validated IP address
 * @throws Error if IP format is invalid
 *
 * @example
 * ```typescript
 * validateIpAddress('192.168.1.1');  // Returns '192.168.1.1'
 * validateIpAddress('192.168.1.256'); // Throws error
 * ```
 */
export function validateIpAddress(ip: string): string {
  const pattern = /^(\d{1,3}\.){3}\d{1,3}$/;
  if (!pattern.test(ip)) {
    throw new Error(`Invalid IP address format: ${ip}. Expected format: x.x.x.x`);
  }
  const octets = ip.split('.');
  for (const octet of octets) {
    const value = parseInt(octet, 10);
    if (value < 0 || value > 255) {
      throw new Error(`Invalid IP address: ${ip}. Each octet must be 0-255.`);
    }
  }
  return ip;
}

/**
 * Validate a file path for basic security
 * Checks for:
 * - Absolute path requirement
 * - Path traversal attempts (..)
 * - Forbidden shell metacharacters
 *
 * @param path - The path to validate
 * @throws Error if path fails validation
 *
 * @example
 * ```typescript
 * validatePath('/tmp/file.txt');        // OK
 * validatePath('/tmp/../etc/passwd');   // Throws error
 * validatePath('/tmp/$(whoami)');       // Throws error
 * ```
 */
export function validatePath(path: string): void {
  if (!path.startsWith('/')) {
    throw new Error(`Path must be absolute: ${path}`);
  }
  if (path.includes('..')) {
    throw new Error(`Path cannot contain ..: ${path}`);
  }
  // Check for shell metacharacters that could enable injection
  if (/[`$(){};|&<>()]/.test(path)) {
    throw new Error(`Path contains forbidden characters: ${path}`);
  }
}

/**
 * Validate a Unix username
 * Only allows alphanumeric characters and underscore
 * Cannot start with a dash or digit
 *
 * @param username - The username to validate
 * @throws Error if username fails validation
 *
 * @example
 * ```typescript
 * validateUsername('root');       // OK
 * validateUsername('valid_user'); // OK
 * validateUsername('-rf');        // Throws error
 * validateUsername('user;rm');    // Throws error
 * ```
 */
export function validateUsername(username: string): void {
  if (username.length === 0) {
    throw new Error('Username cannot be empty');
  }
  // Cannot start with dash (could be interpreted as option)
  if (username.startsWith('-')) {
    throw new Error(`Username starting with dash is not allowed: ${username}`);
  }
  // Only allow alphanumeric and underscore (POSIX username rules)
  if (!/^[a-zA-Z_][a-zA-Z0-9_-]*$/.test(username)) {
    throw new Error(`Invalid username format: ${username}. Only alphanumeric, underscore, and hyphen allowed.`);
  }
}

/**
 * Validate a chmod mode string
 * Accepts octal (e.g., '755', '0755') or symbolic modes (e.g., 'u+x', 'go-w')
 *
 * @param mode - The mode string to validate
 * @throws Error if mode fails validation
 *
 * @example
 * ```typescript
 * validateChmodMode('755');  // OK
 * validateChmodMode('u+x');  // OK
 * validateChmodMode('abc');  // Throws error
 * ```
 */
export function validateChmodMode(mode: string): void {
  if (mode.length === 0) {
    throw new Error('Mode cannot be empty');
  }
  // Octal mode: 3-4 digits (optional leading 0)
  if (/^[0-7]{3,4}$/.test(mode)) {
    return;
  }
  // Symbolic mode: [ugoa...][+-=][rwxXst...]
  // Multiple clauses separated by comma
  if (/^[ugoa]*[+-=][rwxXstugo]*([,][ugoa]*[+-=][rwxXstugo]*)*$/.test(mode)) {
    return;
  }
  throw new Error(`Invalid chmod mode: ${mode}. Expected octal (e.g., 755) or symbolic (e.g., u+x)`);
}