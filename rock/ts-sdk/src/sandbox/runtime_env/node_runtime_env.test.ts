import {
  NodeRuntimeEnvConfig,
  NodeRuntimeEnvConfigSchema,
  NodeRuntimeEnv,
  NODE_DEFAULT_VERSION,
} from './node_runtime_env.js';
import { RuntimeEnvId } from './base.js';
import type { Observation } from '../../types/responses.js';

/**
 * Mock Sandbox for testing
 */
interface MockSandbox {
  sandboxId: string;
  runtimeEnvs: Record<RuntimeEnvId, NodeRuntimeEnv>;
  createSession: jest.Mock;
  arun: jest.Mock;
}

function createMockSandbox(): MockSandbox {
  return {
    sandboxId: 'test-sandbox-id',
    runtimeEnvs: {},
    createSession: jest.fn().mockResolvedValue({}),
    arun: jest.fn().mockResolvedValue({
      output: '',
      exitCode: 0,
      failureReason: '',
      expectString: '',
    } as Observation),
  };
}

describe('NodeRuntimeEnvConfig', () => {
  describe('NodeRuntimeEnvConfigSchema', () => {
    it('should parse valid config with default values', () => {
      const result = NodeRuntimeEnvConfigSchema.safeParse({});
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.type).toBe('node');
        expect(result.data.version).toBe('default');
        expect(result.data.npmRegistry).toBeNull();
        expect(result.data.extraSymlinkExecutables).toEqual(['node', 'npm', 'npx']);
      }
    });

    it('should parse config with version 22.18.0', () => {
      const result = NodeRuntimeEnvConfigSchema.safeParse({ version: '22.18.0' });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.version).toBe('22.18.0');
      }
    });

    it('should reject invalid version', () => {
      const result = NodeRuntimeEnvConfigSchema.safeParse({ version: '20.10.0' });
      expect(result.success).toBe(false);
    });

    it('should parse config with npmRegistry', () => {
      const result = NodeRuntimeEnvConfigSchema.safeParse({
        npmRegistry: 'https://registry.npmmirror.com',
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.npmRegistry).toBe('https://registry.npmmirror.com');
      }
    });

    it('should parse config with custom extraSymlinkExecutables', () => {
      const result = NodeRuntimeEnvConfigSchema.safeParse({
        extraSymlinkExecutables: ['node', 'npm'],
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.extraSymlinkExecutables).toEqual(['node', 'npm']);
      }
    });

    it('should inherit base config fields', () => {
      const result = NodeRuntimeEnvConfigSchema.safeParse({
        version: '22.18.0',
        env: { NODE_ENV: 'production' },
        installTimeout: 900,
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.version).toBe('22.18.0');
        expect(result.data.env).toEqual({ NODE_ENV: 'production' });
        expect(result.data.installTimeout).toBe(900);
      }
    });
  });

  describe('NodeRuntimeEnvConfig type', () => {
    it('should be inferred from schema', () => {
      const config: NodeRuntimeEnvConfig = {
        type: 'node',
        version: 'default',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['node', 'npm', 'npx'],
        npmRegistry: null,
      };
      expect(config.type).toBe('node');
    });
  });

  describe('NODE_DEFAULT_VERSION constant', () => {
    it('should be 22.18.0', () => {
      expect(NODE_DEFAULT_VERSION).toBe('22.18.0');
    });
  });
});

describe('NodeRuntimeEnv', () => {
  let mockSandbox: MockSandbox;

  beforeEach(() => {
    mockSandbox = createMockSandbox();
  });

  describe('constructor', () => {
    it('should create instance with default config', () => {
      const config: NodeRuntimeEnvConfig = {
        type: 'node',
        version: 'default',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['node', 'npm', 'npx'],
        npmRegistry: null,
      };

      const env = new NodeRuntimeEnv(
        mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, NodeRuntimeEnv> },
        config
      );

      expect(env.runtimeEnvType).toBe('node');
      expect(env.initialized).toBe(false);
    });

    it('should throw error for unsupported version', () => {
      const config: NodeRuntimeEnvConfig = {
        type: 'node',
        version: '20.10.0' as '22.18.0', // Force invalid version
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['node', 'npm', 'npx'],
        npmRegistry: null,
      };

      expect(() => {
        new NodeRuntimeEnv(
          mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, NodeRuntimeEnv> },
          config
        );
      }).toThrow('Unsupported Node version');
    });
  });

  describe('_getInstallCmd', () => {
    it('should return node install command', () => {
      const config: NodeRuntimeEnvConfig = {
        type: 'node',
        version: 'default',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['node', 'npm', 'npx'],
        npmRegistry: null,
      };

      const env = new NodeRuntimeEnv(
        mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, NodeRuntimeEnv> },
        config
      );

      const installCmd = (env as unknown as { _getInstallCmd: () => string })._getInstallCmd();
      expect(installCmd).toContain('node');
    });
  });

  describe('init', () => {
    it('should validate node exists after installation', async () => {
      const config: NodeRuntimeEnvConfig = {
        type: 'node',
        version: 'default',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['node', 'npm', 'npx'],
        npmRegistry: null,
      };

      const env = new NodeRuntimeEnv(
        mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, NodeRuntimeEnv> },
        config
      );

      await env.init();

      // Check that test -x node was called
      const calls = mockSandbox.arun.mock.calls;
      const validateCall = calls.find((call: unknown[]) =>
        typeof call[0] === 'string' && call[0].includes('test -x node')
      );
      expect(validateCall).toBeDefined();
    });

    it('should configure npm registry if specified', async () => {
      const config: NodeRuntimeEnvConfig = {
        type: 'node',
        version: 'default',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['node', 'npm', 'npx'],
        npmRegistry: 'https://registry.npmmirror.com',
      };

      const env = new NodeRuntimeEnv(
        mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, NodeRuntimeEnv> },
        config
      );

      await env.init();

      // Check that npm config was called
      const calls = mockSandbox.arun.mock.calls;
      const npmConfigCall = calls.find((call: unknown[]) =>
        typeof call[0] === 'string' && call[0].includes('npm config set registry')
      );
      expect(npmConfigCall).toBeDefined();
    });
  });
});
