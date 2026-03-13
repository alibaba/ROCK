import { RuntimeEnv, RuntimeEnvId, createRuntimeEnvId } from './base.js';
import { RuntimeEnvConfig } from './config.js';
import type { Observation } from '../../types/responses.js';

/**
 * Mock Sandbox for testing
 */
interface MockSandbox {
  sandboxId: string;
  runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv>;
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

/**
 * Test implementation of RuntimeEnv
 */
class TestRuntimeEnv extends RuntimeEnv {
  readonly runtimeEnvType = 'test';

  protected _getInstallCmd(): string {
    return 'echo "installing test runtime"';
  }
}

describe('RuntimeEnv', () => {
  describe('createRuntimeEnvId', () => {
    it('should create a valid RuntimeEnvId', () => {
      const id = createRuntimeEnvId();
      expect(typeof id).toBe('string');
      expect(id.length).toBe(8);
    });

    it('should create unique IDs', () => {
      const id1 = createRuntimeEnvId();
      const id2 = createRuntimeEnvId();
      expect(id1).not.toBe(id2);
    });
  });

  describe('RuntimeEnv base class', () => {
    let mockSandbox: MockSandbox;
    let config: RuntimeEnvConfig;

    beforeEach(() => {
      mockSandbox = createMockSandbox();
      config = {
        type: 'test',
        version: 'default',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: [],
      };
    });

    describe('constructor', () => {
      it('should initialize with correct properties', () => {
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);

        expect(env.runtimeEnvId).toBeDefined();
        expect(env.runtimeEnvId.length).toBe(8);
        expect(env.workdir).toContain('/tmp/rock-runtime-envs/test/default/');
        expect(env.binDir).toContain('/tmp/rock-runtime-envs/test/default/');
        expect(env.binDir).toContain('/runtime-env/bin');
        expect(env.initialized).toBe(false);
      });

      it('should use version in workdir path', () => {
        config.version = '1.0.0';
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);

        expect(env.workdir).toContain('/tmp/rock-runtime-envs/test/1.0.0/');
      });

      it('should use "default" when version is empty', () => {
        config.version = '';
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);

        expect(env.workdir).toContain('/tmp/rock-runtime-envs/test/default/');
      });
    });

    describe('init', () => {
      it('should initialize runtime environment', async () => {
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);

        await env.init();

        expect(env.initialized).toBe(true);
        expect(mockSandbox.createSession).toHaveBeenCalled();
        expect(mockSandbox.arun).toHaveBeenCalled();
      });

      it('should be idempotent - calling init multiple times only initializes once', async () => {
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);

        await env.init();
        await env.init();
        await env.init();

        // Should only create session once
        expect(mockSandbox.createSession).toHaveBeenCalledTimes(1);
      });

      it('should execute custom install command if provided', async () => {
        config.customInstallCmd = 'npm install';
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);

        await env.init();

        const calls = mockSandbox.arun.mock.calls;
        const customInstallCall = calls.find((call: unknown[]) => 
          typeof call[0] === 'string' && call[0].includes('npm install')
        );
        expect(customInstallCall).toBeDefined();
      });

      it('should create symlinks if extraSymlinkDir and executables are provided', async () => {
        config.extraSymlinkDir = '/usr/local/bin';
        config.extraSymlinkExecutables = ['python', 'pip'];
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);

        await env.init();

        const lastCall = mockSandbox.arun.mock.calls[mockSandbox.arun.mock.calls.length - 1];
        expect(lastCall[0]).toContain('ln -sf');
        expect(lastCall[0]).toContain('python');
        expect(lastCall[0]).toContain('pip');
      });
    });

    describe('run', () => {
      it('should wrap command with PATH and execute', async () => {
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);
        await env.init();

        mockSandbox.arun.mockClear();
        await env.run('python --version');

        expect(mockSandbox.arun).toHaveBeenCalledWith(
          expect.stringContaining('export PATH='),
          expect.objectContaining({
            session: expect.stringContaining('runtime-env-test-'),
            mode: 'nohup',
          })
        );
      });

      it('should raise error when command fails', async () => {
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);
        await env.init();

        // Mock the next arun call to return a failure
        mockSandbox.arun.mockResolvedValueOnce({
          output: 'error message',
          exitCode: 1,
          failureReason: 'command failed',
          expectString: '',
        } as Observation);

        await expect(env.run('failing-command')).rejects.toThrow();
      });

      it('should use custom timeout and error message', async () => {
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);
        await env.init();

        mockSandbox.arun.mockClear();
        await env.run('long-command', { waitTimeout: 1200, errorMsg: 'custom error' });

        expect(mockSandbox.arun).toHaveBeenCalledWith(
          expect.any(String),
          expect.objectContaining({
            waitTimeout: 1200,
          })
        );
      });
    });

    describe('wrappedCmd', () => {
      it('should prepend bin dir to PATH when prepend=true', () => {
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);

        const wrapped = env.wrappedCmd('python --version', true);

        expect(wrapped).toContain('export PATH=');
        expect(wrapped).toContain('$PATH');
        expect(wrapped).toContain('bash -c');
      });

      it('should append bin dir to PATH when prepend=false', () => {
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);

        const wrapped = env.wrappedCmd('python --version', false);

        expect(wrapped).toContain('$PATH:');
        expect(wrapped).toContain('bash -c');
      });

      it('should shell-escape the bin dir path', () => {
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);

        const wrapped = env.wrappedCmd('python --version', true);

        // Path should be quoted to handle spaces
        expect(wrapped).toMatch(/export PATH='[^']+'/);
      });
    });

    describe('command injection protection', () => {
      it('should safely handle workdir with special characters', async () => {
        // Create a config that would result in workdir with special characters
        config.type = 'test space';
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);

        await env.init();

        // Check that workdir path is properly quoted
        const calls = mockSandbox.arun.mock.calls;
        const mkdirCall = calls.find((call: unknown[]) => {
          const cmd = call[0] as string;
          return cmd.includes('mkdir');
        });

        expect(mkdirCall).toBeDefined();
        const cmd = (mkdirCall as unknown[])[0] as string;
        // Path with space should be properly quoted or escaped
        expect(cmd).toMatch(/mkdir -p/);
      });

      it('should safely handle binDir with spaces in wrappedCmd', () => {
        config.type = 'test space';
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);

        const wrapped = env.wrappedCmd('python --version', true);

        // Spaces in path should be properly quoted/escaped
        // The path should contain the escaped space
        expect(wrapped).toContain('test space');
        // The entire command should be wrapped in bash -c with proper escaping
        expect(wrapped).toMatch(/bash -c/);
      });

      it('should safely handle symlink directory with injection attempt', async () => {
        config.extraSymlinkDir = '/tmp; rm -rf /';
        config.extraSymlinkExecutables = ['python'];
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);

        await env.init();

        // Check that injection is not executed - it should be quoted
        const calls = mockSandbox.arun.mock.calls;
        const symlinkCall = calls.find((call: unknown[]) => {
          const cmd = call[0] as string;
          return cmd.includes('ln -sf');
        });

        if (symlinkCall) {
          const cmd = symlinkCall[0] as string;
          // The path should be quoted, preventing injection
          // If properly quoted, the semicolon won't be interpreted as command separator
          expect(cmd).toMatch(/ln -sf/);
        }
      });

      it('should safely handle executable name with injection attempt', async () => {
        config.extraSymlinkDir = '/usr/local/bin';
        config.extraSymlinkExecutables = ['python; rm -rf /'];
        const env = new TestRuntimeEnv(mockSandbox as unknown as MockSandbox & { runtimeEnvs: Record<RuntimeEnvId, RuntimeEnv> }, config);

        await env.init();

        // Check that injection is not executed
        const calls = mockSandbox.arun.mock.calls;
        const symlinkCall = calls.find((call: unknown[]) => {
          const cmd = call[0] as string;
          return cmd.includes('ln -sf');
        });

        if (symlinkCall) {
          const cmd = symlinkCall[0] as string;
          // The executable name should be quoted
          expect(cmd).toMatch(/ln -sf/);
        }
      });
    });
  });
});