/**
 * Tests for Sandbox constants
 * 
 * Tests ENSURE_OSSUTIL_SCRIPT and other sandbox-related constants
 */

import { ENSURE_OSSUTIL_SCRIPT } from './constants.js';

describe('ENSURE_OSSUTIL_SCRIPT', () => {
  test('should be a non-empty string', () => {
    expect(ENSURE_OSSUTIL_SCRIPT).toBeTruthy();
    expect(typeof ENSURE_OSSUTIL_SCRIPT).toBe('string');
    expect(ENSURE_OSSUTIL_SCRIPT.length).toBeGreaterThan(0);
  });

  test('should start with bash shebang', () => {
    expect(ENSURE_OSSUTIL_SCRIPT.startsWith('#!/bin/bash')).toBe(true);
  });

  test('should contain wget download URL for ossutil', () => {
    expect(ENSURE_OSSUTIL_SCRIPT).toContain('https://gosspublic.alicdn.com/ossutil/v2/2.2.1/ossutil-2.2.1-linux-amd64.zip');
  });

  test('should check for wget or curl availability', () => {
    expect(ENSURE_OSSUTIL_SCRIPT).toContain('command -v wget');
    expect(ENSURE_OSSUTIL_SCRIPT).toContain('command -v curl');
  });

  test('should use Python to extract zip (more universal than unzip)', () => {
    expect(ENSURE_OSSUTIL_SCRIPT).toContain('python3 -c "import zipfile');
    expect(ENSURE_OSSUTIL_SCRIPT).toContain('python -c "import zipfile');
  });

  test('should skip installation if ossutil already exists', () => {
    expect(ENSURE_OSSUTIL_SCRIPT).toContain('command -v ossutil');
    expect(ENSURE_OSSUTIL_SCRIPT).toContain('ossutil already installed');
  });

  test('should install to /usr/local/bin', () => {
    expect(ENSURE_OSSUTIL_SCRIPT).toContain('/usr/local/bin');
  });

  test('should verify installation with ossutil version', () => {
    expect(ENSURE_OSSUTIL_SCRIPT).toContain('ossutil version');
  });

  test('should cleanup temporary files', () => {
    expect(ENSURE_OSSUTIL_SCRIPT).toContain('rm -rf');
    expect(ENSURE_OSSUTIL_SCRIPT).toContain('/tmp/ossutil.zip');
  });
});
