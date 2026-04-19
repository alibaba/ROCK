/**
 * Integration test for Sandbox lifecycle
 * 
 * Prerequisites:
 * - ROCK_BASE_URL environment variable or default baseUrl
 * - Access to the ROCK sandbox service
 * - Valid Docker image
 */

import { Sandbox } from '../../src/sandbox/client.js';

const TEST_CONFIG = {
  baseUrl: process.env.ROCK_BASE_URL || 'http://11.166.8.116:8080',
  image: 'reg.docker.alibaba-inc.com/yanan/python:3.11',
  cluster: 'zb',
  startupTimeout: 120, // 2 minutes timeout for sandbox startup
};

describe('Sandbox Lifecycle Integration', () => {
  let sandbox: Sandbox;

  beforeEach(() => {
    sandbox = new Sandbox(TEST_CONFIG);
  });

  afterEach(async () => {
    // Cleanup: ensure sandbox is stopped even if test fails
    if (sandbox) {
      try {
        await sandbox.close();
      } catch (e) {
        // Ignore cleanup errors
      }
    }
  });

  test('should start sandbox, wait for isAlive=true, then stop sandbox', async () => {
    // Step 1: Start sandbox
    await sandbox.start();

    // Step 2: Verify sandbox is alive
    const aliveResponse = await sandbox.isAlive();
    expect(aliveResponse.isAlive).toBe(true);

    // Step 3: Stop sandbox
    await sandbox.stop();

    // Step 4: Verify sandbox is stopped (isAlive should be false or throw error)
    try {
      const afterStopResponse = await sandbox.isAlive();
      expect(afterStopResponse.isAlive).toBe(false);
    } catch (e) {
      // After stop, isAlive may throw error, which is acceptable
      expect(String(e)).toContain('Failed to get is alive');
    }
  }, 180000); // 3 minutes timeout for the whole test
});
