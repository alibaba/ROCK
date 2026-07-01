/**
 * Tests for environment variables configuration
 */

import { envVars } from './env_vars.js';

describe('envVars', () => {
  describe('PyPI configuration', () => {
    test('ROCK_PIP_INDEX_URL should default to Aliyun PyPI mirror matching Python SDK', () => {
      expect(envVars.ROCK_PIP_INDEX_URL).toBe('https://mirrors.aliyun.com/pypi/simple/');
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
      expect(envVars.ROCK_DEFAULT_CLUSTER).toBe('vpc-nt-a');
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

  describe('Service status directory', () => {
    test('ROCK_SERVICE_STATUS_DIR should default to /tmp', () => {
      expect(envVars.ROCK_SERVICE_STATUS_DIR).toBe('/tmp');
    });
  });

  describe('Newly added env vars (matching Python SDK)', () => {
    test('ROCK_FORCE_PRIMARY_POD should default to false', () => {
      expect(envVars.ROCK_FORCE_PRIMARY_POD).toBe(false);
    });

    test('ROCK_DOCKER_TEMP_AUTH_DIR should default to undefined', () => {
      expect(envVars.ROCK_DOCKER_TEMP_AUTH_DIR).toBeUndefined();
    });

    test('ROCK_JOB_PROXY_REPLAY_FILE should have default value', () => {
      expect(envVars.ROCK_JOB_PROXY_REPLAY_FILE).toBe(
        '/data/logs/user-defined/rock-job-proxy-replay.jsonl'
      );
    });

    test('ROCK_BASH_JOB_ARTIFACT_DIR should have default value', () => {
      expect(envVars.ROCK_BASH_JOB_ARTIFACT_DIR).toBe('/data/logs/user-defined');
    });

    test('ROCK_OSS_TRANSFER_PREFIX should default to undefined', () => {
      expect(envVars.ROCK_OSS_TRANSFER_PREFIX).toBeUndefined();
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
