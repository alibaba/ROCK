/**
 * Tests for Network - Network management for sandbox
 */

import { Network } from './network.js';
import { SpeedupType } from './types.js';
import type { Sandbox } from './client.js';

describe('Network', () => {
  describe('buildAptSpeedupCommand', () => {
    // Test the command generation by accessing through a test helper
    // We need to verify the heredoc syntax allows shell variable expansion
    test('should use unquoted heredoc delimiter to allow shell variable expansion', () => {
      // Create a mock sandbox
      const mockSandbox = {
        getSandboxId: () => 'test-sandbox',
        arun: jest.fn().mockResolvedValue({
          output: '',
          exitCode: 0,
          failureReason: '',
          expectString: '',
        }),
      } as unknown as Sandbox;

      const network = new Network(mockSandbox);
      const mirrorUrl = 'http://mirrors.aliyun.com/ubuntu';

      // Call speedup which internally uses buildAptSpeedupCommand
      network.speedup(SpeedupType.APT, mirrorUrl);

      // Get the command that was passed to arun
      const callArgs = (mockSandbox.arun as jest.Mock).mock.calls[0];
      const command = callArgs[0] as string;

      // The command should use unquoted heredoc (<< EOF) not quoted (<< 'EOF')
      // This allows $(lsb_release -cs) to be expanded by the shell
      expect(command).toContain('<< EOF');
      expect(command).not.toContain("<< 'EOF'");
    });

    test('should include lsb_release command for dynamic codename', () => {
      const mockSandbox = {
        getSandboxId: () => 'test-sandbox',
        arun: jest.fn().mockResolvedValue({
          output: '',
          exitCode: 0,
          failureReason: '',
          expectString: '',
        }),
      } as unknown as Sandbox;

      const network = new Network(mockSandbox);
      const mirrorUrl = 'http://mirrors.aliyun.com/ubuntu';

      network.speedup(SpeedupType.APT, mirrorUrl);

      const callArgs = (mockSandbox.arun as jest.Mock).mock.calls[0];
      const command = callArgs[0] as string;

      // Verify the command includes $(lsb_release -cs) for dynamic expansion
      expect(command).toContain('$(lsb_release -cs)');
    });
  });
});
