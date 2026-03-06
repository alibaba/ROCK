/**
 * Tests for Network - Network management for sandbox
 */

import { Network, SpeedupType } from './network.js';
import type { Observation } from '../types/responses.js';

/**
 * Mock types for testing
 */
interface MockProcess {
  executeScript: jest.Mock;
}

interface MockSandbox {
  getSandboxId: () => string;
  arun: jest.Mock;
  getProcess: () => MockProcess;
}

/**
 * Create a mock sandbox with all required methods
 */
function createMockSandbox(): MockSandbox {
  const mockProcess: MockProcess = {
    executeScript: jest.fn().mockResolvedValue({
      output: '',
      exitCode: 0,
      failureReason: '',
      expectString: '',
    } as Observation),
  };

  return {
    getSandboxId: () => 'test-sandbox',
    arun: jest.fn().mockResolvedValue({
      output: '',
      exitCode: 0,
      failureReason: '',
      expectString: '',
    } as Observation),
    getProcess: () => mockProcess,
  };
}

describe('Network', () => {
  describe('buildAptSpeedupScript', () => {
    // Test the script generation for APT speedup
    test('should generate valid bash script with heredoc', async () => {
      const mockSandbox = createMockSandbox();
      const network = new Network(mockSandbox as unknown as import('./client.js').Sandbox);
      const mirrorUrl = 'http://mirrors.aliyun.com/ubuntu';

      // Call speedup which internally uses buildAptSpeedupScript
      await network.speedup(SpeedupType.APT, mirrorUrl);

      // Get the script content that was passed to executeScript
      const executeScriptMock = mockSandbox.getProcess().executeScript;
      const callArgs = executeScriptMock.mock.calls[0];
      const options = callArgs[0] as { scriptContent: string };
      const scriptContent = options.scriptContent;

      // The script should be a valid bash script
      expect(scriptContent).toContain('#!/bin/bash');
      // Should use heredoc for writing sources.list
      expect(scriptContent).toContain('<<EOF');
      // Should NOT use quoted heredoc which would prevent variable expansion
      expect(scriptContent).not.toContain("<<'EOF'");
    });

    test('should include system detection for dynamic codename', async () => {
      const mockSandbox = createMockSandbox();
      const network = new Network(mockSandbox as unknown as import('./client.js').Sandbox);
      const mirrorUrl = 'http://mirrors.aliyun.com/ubuntu';

      await network.speedup(SpeedupType.APT, mirrorUrl);

      const executeScriptMock = mockSandbox.getProcess().executeScript;
      const callArgs = executeScriptMock.mock.calls[0];
      const options = callArgs[0] as { scriptContent: string };
      const scriptContent = options.scriptContent;

      // Verify the script includes system detection function
      expect(scriptContent).toContain('detect_system_and_version');
      expect(scriptContent).toContain('VERSION_CODENAME');
    });

    test('should include validated mirror URL in script', async () => {
      const mockSandbox = createMockSandbox();
      const network = new Network(mockSandbox as unknown as import('./client.js').Sandbox);
      const mirrorUrl = 'http://mirrors.aliyun.com/ubuntu';

      await network.speedup(SpeedupType.APT, mirrorUrl);

      const executeScriptMock = mockSandbox.getProcess().executeScript;
      const callArgs = executeScriptMock.mock.calls[0];
      const options = callArgs[0] as { scriptContent: string };
      const scriptContent = options.scriptContent;

      // Verify the validated URL is in the script
      expect(scriptContent).toContain(mirrorUrl);
    });
  });

  describe('command injection protection', () => {
    describe('GitHub speedup', () => {
      it('should reject invalid IP address format', async () => {
        const mockSandbox = createMockSandbox();
        const network = new Network(mockSandbox as unknown as import('./client.js').Sandbox);

        // IP with injection attempt
        await expect(network.speedup(SpeedupType.GITHUB, '1.1.1.1; rm -rf /')).rejects.toThrow();
        await expect(network.speedup(SpeedupType.GITHUB, 'not-an-ip')).rejects.toThrow();
        await expect(network.speedup(SpeedupType.GITHUB, '256.1.1.1')).rejects.toThrow();
      });

      it('should accept valid IP address', async () => {
        const mockSandbox = createMockSandbox();
        const network = new Network(mockSandbox as unknown as import('./client.js').Sandbox);

        // Should not throw for valid IP
        await expect(network.speedup(SpeedupType.GITHUB, '192.168.1.1')).resolves.toBeDefined();

        // Verify the script doesn't have unquoted injection
        const executeScriptMock = mockSandbox.getProcess().executeScript;
        const callArgs = executeScriptMock.mock.calls[0];
        const options = callArgs[0] as { scriptContent: string };
        expect(options.scriptContent).not.toMatch(/; rm -rf/);
      });
    });

    describe('APT speedup', () => {
      it('should reject invalid URL format', async () => {
        const mockSandbox = createMockSandbox();
        const network = new Network(mockSandbox as unknown as import('./client.js').Sandbox);

        // Invalid URLs should be rejected
        await expect(network.speedup(SpeedupType.APT, 'not-a-url')).rejects.toThrow();
        await expect(network.speedup(SpeedupType.APT, 'ftp://evil.com')).rejects.toThrow();
      });

      it('should accept valid HTTP/HTTPS URLs', async () => {
        const mockSandbox = createMockSandbox();
        const network = new Network(mockSandbox as unknown as import('./client.js').Sandbox);

        await expect(network.speedup(SpeedupType.APT, 'http://mirrors.aliyun.com')).resolves.toBeDefined();
        await expect(network.speedup(SpeedupType.APT, 'https://mirrors.aliyun.com')).resolves.toBeDefined();
      });
    });

    describe('PIP speedup', () => {
      it('should reject invalid URL format', async () => {
        const mockSandbox = createMockSandbox();
        const network = new Network(mockSandbox as unknown as import('./client.js').Sandbox);

        await expect(network.speedup(SpeedupType.PIP, 'javascript:alert(1)')).rejects.toThrow();
        await expect(network.speedup(SpeedupType.PIP, 'not-a-url')).rejects.toThrow();
      });

      it('should accept valid HTTP/HTTPS URLs', async () => {
        const mockSandbox = createMockSandbox();
        const network = new Network(mockSandbox as unknown as import('./client.js').Sandbox);

        await expect(network.speedup(SpeedupType.PIP, 'http://mirrors.aliyun.com/pypi/simple/')).resolves.toBeDefined();
      });
    });
  });
});