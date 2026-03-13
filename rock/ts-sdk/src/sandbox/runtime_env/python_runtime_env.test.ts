import {
  PythonRuntimeEnvConfig,
  PythonRuntimeEnvConfigSchema,
  PythonRuntimeEnv,
} from './python_runtime_env.js';
import { RuntimeEnvId } from './base.js';
import type { Observation } from '../../types/responses.js';

/**
 * Mock Sandbox for testing
 */
interface MockSandbox {
  sandboxId: string;
  runtimeEnvs: Record<RuntimeEnvId, PythonRuntimeEnv>;
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

describe('PythonRuntimeEnvConfig', () => {
  describe('PythonRuntimeEnvConfigSchema', () => {
    it('should parse valid config with default values', () => {
      const result = PythonRuntimeEnvConfigSchema.safeParse({});
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.type).toBe('python');
        expect(result.data.version).toBe('default');
        expect(result.data.pip).toBeNull();
        expect(result.data.pipIndexUrl).toBeNull();
        expect(result.data.extraSymlinkExecutables).toEqual(['python', 'python3', 'pip', 'pip3']);
      }
    });

    it('should parse config with version 3.11', () => {
      const result = PythonRuntimeEnvConfigSchema.safeParse({ version: '3.11' });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.version).toBe('3.11');
      }
    });

    it('should parse config with version 3.12', () => {
      const result = PythonRuntimeEnvConfigSchema.safeParse({ version: '3.12' });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.version).toBe('3.12');
      }
    });

    it('should reject invalid version', () => {
      const result = PythonRuntimeEnvConfigSchema.safeParse({ version: '3.10' });
      expect(result.success).toBe(false);
    });

    it('should parse config with pip packages as array', () => {
      const result = PythonRuntimeEnvConfigSchema.safeParse({
        pip: ['langchain', 'langchain-openai'],
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.pip).toEqual(['langchain', 'langchain-openai']);
      }
    });

    it('should parse config with pip as requirements.txt path', () => {
      const result = PythonRuntimeEnvConfigSchema.safeParse({
        pip: './requirements.txt',
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.pip).toBe('./requirements.txt');
      }
    });

    it('should parse config with pipIndexUrl', () => {
      const result = PythonRuntimeEnvConfigSchema.safeParse({
        pipIndexUrl: 'https://mirrors.aliyun.com/pypi/simple/',
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.pipIndexUrl).toBe('https://mirrors.aliyun.com/pypi/simple/');
      }
    });

    it('should parse config with custom extraSymlinkExecutables', () => {
      const result = PythonRuntimeEnvConfigSchema.safeParse({
        extraSymlinkExecutables: ['python', 'pip'],
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.extraSymlinkExecutables).toEqual(['python', 'pip']);
      }
    });

    it('should inherit base config fields', () => {
      const result = PythonRuntimeEnvConfigSchema.safeParse({
        version: '3.12',
        env: { PYTHONPATH: '/app' },
        installTimeout: 900,
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.version).toBe('3.12');
        expect(result.data.env).toEqual({ PYTHONPATH: '/app' });
        expect(result.data.installTimeout).toBe(900);
      }
    });
  });

  describe('PythonRuntimeEnvConfig type', () => {
    it('should be inferred from schema', () => {
      const config: PythonRuntimeEnvConfig = {
        type: 'python',
        version: 'default',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['python', 'python3', 'pip', 'pip3'],
        pip: null,
        pipIndexUrl: null,
      };
      expect(config.type).toBe('python');
    });
  });
});

describe('PythonRuntimeEnv', () => {
  let mockSandbox: MockSandbox;

  beforeEach(() => {
    mockSandbox = createMockSandbox();
  });

  describe('constructor', () => {
    it('should create instance with default config', () => {
      const config: PythonRuntimeEnvConfig = {
        type: 'python',
        version: 'default',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['python', 'python3', 'pip', 'pip3'],
        pip: null,
        pipIndexUrl: null,
      };

      const env = new PythonRuntimeEnv(
        mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, PythonRuntimeEnv> },
        config
      );

      expect(env.runtimeEnvType).toBe('python');
      expect(env.initialized).toBe(false);
    });

    it('should throw error for unsupported version', () => {
      const config: PythonRuntimeEnvConfig = {
        type: 'python',
        version: '3.10' as '3.11', // Force invalid version
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['python', 'python3', 'pip', 'pip3'],
        pip: null,
        pipIndexUrl: null,
      };

      expect(() => {
        new PythonRuntimeEnv(
          mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, PythonRuntimeEnv> },
          config
        );
      }).toThrow('Unsupported Python version');
    });
  });

  describe('_getInstallCmd', () => {
    it('should return 3.11 install command for version 3.11', () => {
      const config: PythonRuntimeEnvConfig = {
        type: 'python',
        version: '3.11',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['python', 'python3', 'pip', 'pip3'],
        pip: null,
        pipIndexUrl: null,
      };

      const env = new PythonRuntimeEnv(
        mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, PythonRuntimeEnv> },
        config
      );

      // Access protected method via type assertion
      const installCmd = (env as unknown as { _getInstallCmd: () => string })._getInstallCmd();
      expect(installCmd).toContain('cpython-3.11');
    });

    it('should return 3.12 install command for version 3.12', () => {
      const config: PythonRuntimeEnvConfig = {
        type: 'python',
        version: '3.12',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['python', 'python3', 'pip', 'pip3'],
        pip: null,
        pipIndexUrl: null,
      };

      const env = new PythonRuntimeEnv(
        mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, PythonRuntimeEnv> },
        config
      );

      const installCmd = (env as unknown as { _getInstallCmd: () => string })._getInstallCmd();
      expect(installCmd).toContain('cpython-3.12');
    });

    it('should return 3.11 install command for default version', () => {
      const config: PythonRuntimeEnvConfig = {
        type: 'python',
        version: 'default',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['python', 'python3', 'pip', 'pip3'],
        pip: null,
        pipIndexUrl: null,
      };

      const env = new PythonRuntimeEnv(
        mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, PythonRuntimeEnv> },
        config
      );

      const installCmd = (env as unknown as { _getInstallCmd: () => string })._getInstallCmd();
      expect(installCmd).toContain('cpython-3.11');
    });
  });

  describe('init', () => {
    it('should validate python exists after installation', async () => {
      const config: PythonRuntimeEnvConfig = {
        type: 'python',
        version: 'default',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['python', 'python3', 'pip', 'pip3'],
        pip: null,
        pipIndexUrl: null,
      };

      const env = new PythonRuntimeEnv(
        mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, PythonRuntimeEnv> },
        config
      );

      await env.init();

      // Check that test -x python was called
      const calls = mockSandbox.arun.mock.calls;
      const validateCall = calls.find((call: unknown[]) =>
        typeof call[0] === 'string' && call[0].includes('test -x python')
      );
      expect(validateCall).toBeDefined();
    });

    it('should configure pip index url if specified', async () => {
      const config: PythonRuntimeEnvConfig = {
        type: 'python',
        version: 'default',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['python', 'python3', 'pip', 'pip3'],
        pip: null,
        pipIndexUrl: 'https://mirrors.aliyun.com/pypi/simple/',
      };

      const env = new PythonRuntimeEnv(
        mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, PythonRuntimeEnv> },
        config
      );

      await env.init();

      // Check that pip config was called
      const calls = mockSandbox.arun.mock.calls;
      const pipConfigCall = calls.find((call: unknown[]) =>
        typeof call[0] === 'string' && call[0].includes('pip config set global.index-url')
      );
      expect(pipConfigCall).toBeDefined();
    });

    it('should install pip packages if specified as array', async () => {
      const config: PythonRuntimeEnvConfig = {
        type: 'python',
        version: 'default',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: ['python', 'python3', 'pip', 'pip3'],
        pip: ['langchain', 'langchain-openai'],
        pipIndexUrl: null,
      };

      const env = new PythonRuntimeEnv(
        mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, PythonRuntimeEnv> },
        config
      );

      await env.init();

      // Check that pip install was called
      const calls = mockSandbox.arun.mock.calls;
      const pipInstallCall = calls.find((call: unknown[]) =>
        typeof call[0] === 'string' && call[0].includes('pip install')
      );
      expect(pipInstallCall).toBeDefined();
      expect(pipInstallCall?.[0]).toContain('langchain');
    });
  });
});
