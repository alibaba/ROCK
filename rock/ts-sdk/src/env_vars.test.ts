/**
 * Tests for environment variables configuration
 */

import { envVars } from './env_vars.js';

describe('envVars', () => {
  describe('PyPI configuration', () => {
    test('ROCK_PIP_INDEX_URL should default to public PyPI mirror for open-source SDK', () => {
      expect(envVars.ROCK_PIP_INDEX_URL).toBe('https://pypi.org/simple/');
    });
  });

  describe('Python install command URLs', () => {
    test('V31114 install command URL should contain releases/download/ path', () => {
      const cmd = envVars.ROCK_RTENV_PYTHON_V31114_INSTALL_CMD;
      expect(cmd).toContain('releases/download/');
    });

    test('V31212 install command URL should contain releases/download/ path', () => {
      const cmd = envVars.ROCK_RTENV_PYTHON_V31212_INSTALL_CMD;
      expect(cmd).toContain('releases/download/');
    });
  });

  describe('Sandbox default configuration', () => {
    test('ROCK_DEFAULT_IMAGE should have default value', () => {
      expect(envVars.ROCK_DEFAULT_IMAGE).toBe('python:3.11');
    });

    test('ROCK_DEFAULT_MEMORY should have default value', () => {
      expect(envVars.ROCK_DEFAULT_MEMORY).toBe('8g');
    });

    test('ROCK_DEFAULT_CPUS should have default value', () => {
      expect(envVars.ROCK_DEFAULT_CPUS).toBe(2);
    });

    test('ROCK_DEFAULT_CLUSTER should have default value', () => {
      expect(envVars.ROCK_DEFAULT_CLUSTER).toBe('zb');
    });

    test('ROCK_DEFAULT_AUTO_CLEAR_SECONDS should have default value', () => {
      expect(envVars.ROCK_DEFAULT_AUTO_CLEAR_SECONDS).toBe(300);
    });
  });

  describe('SandboxGroup default configuration', () => {
    test('ROCK_DEFAULT_GROUP_SIZE should have default value', () => {
      expect(envVars.ROCK_DEFAULT_GROUP_SIZE).toBe(2);
    });

    test('ROCK_DEFAULT_START_CONCURRENCY should have default value', () => {
      expect(envVars.ROCK_DEFAULT_START_CONCURRENCY).toBe(2);
    });

    test('ROCK_DEFAULT_START_RETRY_TIMES should have default value', () => {
      expect(envVars.ROCK_DEFAULT_START_RETRY_TIMES).toBe(3);
    });
  });

  describe('Client timeout defaults', () => {
    test('ROCK_DEFAULT_ARUN_TIMEOUT should have default value', () => {
      expect(envVars.ROCK_DEFAULT_ARUN_TIMEOUT).toBe(300);
    });

    test('ROCK_DEFAULT_NOHUP_WAIT_TIMEOUT should have default value', () => {
      expect(envVars.ROCK_DEFAULT_NOHUP_WAIT_TIMEOUT).toBe(300);
    });

    test('ROCK_DEFAULT_NOHUP_WAIT_INTERVAL should have default value', () => {
      expect(envVars.ROCK_DEFAULT_NOHUP_WAIT_INTERVAL).toBe(10);
    });

    test('ROCK_DEFAULT_STATUS_CHECK_INTERVAL should have default value', () => {
      expect(envVars.ROCK_DEFAULT_STATUS_CHECK_INTERVAL).toBe(3);
    });
  });
});
