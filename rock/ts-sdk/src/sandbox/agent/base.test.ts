import { Agent, DefaultAgent } from './base.js';
import { RockAgentConfigSchema } from './config.js';
import type { SandboxLike, RuntimeEnv } from '../runtime_env/base.js';
import type { Observation } from '../../types/responses.js';

// Mock sandbox for testing
const createMockSandbox = (): SandboxLike => ({
  sandboxId: 'test-sandbox-id',
  runtimeEnvs: {} as Record<string, RuntimeEnv>,
  arun: jest.fn().mockResolvedValue({ exitCode: 0, output: 'success', failureReason: '', expectString: '' } as Observation),
  createSession: jest.fn().mockResolvedValue(undefined),
  startNohupProcess: jest.fn().mockResolvedValue({ pid: 12345, errorResponse: null }),
  waitForProcessCompletion: jest.fn().mockResolvedValue({ success: true, message: 'completed' }),
  handleNohupOutput: jest.fn().mockResolvedValue({
    output: 'test output',
    exitCode: 0,
    failureReason: '',
    expectString: '',
  } as Observation),
});

describe('Agent', () => {
  describe('Agent base class', () => {
    it('should be abstract and require sandbox', () => {
      const mockSandbox = createMockSandbox();
      const agent = new DefaultAgent(mockSandbox);
      expect(agent.sandbox).toBe(mockSandbox);
    });
  });
});

describe('DefaultAgent', () => {
  describe('constructor', () => {
    it('should create agent with sandbox', () => {
      const mockSandbox = createMockSandbox();
      const agent = new DefaultAgent(mockSandbox);
      expect(agent.sandbox).toBe(mockSandbox);
      expect(agent.modelService).toBeNull();
      expect(agent.config).toBeNull();
      expect(agent.agentSession).toBeNull();
    });
  });

  describe('install', () => {
    it('should initialize agent with config', async () => {
      const mockSandbox = createMockSandbox();
      const agent = new DefaultAgent(mockSandbox);

      const configResult = RockAgentConfigSchema.safeParse({
        agentSession: 'test-session',
        env: { TEST_VAR: 'test_value' },
      });
      expect(configResult.success).toBe(true);

      if (configResult.success) {
        await agent.install(configResult.data);

        expect(agent.config).toBe(configResult.data);
        expect(agent.agentSession).toBe('test-session');
        expect(mockSandbox.createSession).toHaveBeenCalled();
      }
    });

    it('should execute pre-init commands', async () => {
      const mockSandbox = createMockSandbox();
      const agent = new DefaultAgent(mockSandbox);

      const configResult = RockAgentConfigSchema.safeParse({
        agentSession: 'test-session',
        preInitCmds: [{ command: 'echo pre-init', timeoutSeconds: 60 }],
      });
      expect(configResult.success).toBe(true);

      if (configResult.success) {
        await agent.install(configResult.data);
        expect(mockSandbox.arun).toHaveBeenCalled();
      }
    });

    it('should execute post-init commands', async () => {
      const mockSandbox = createMockSandbox();
      const agent = new DefaultAgent(mockSandbox);

      const configResult = RockAgentConfigSchema.safeParse({
        agentSession: 'test-session',
        postInitCmds: [{ command: 'echo post-init', timeoutSeconds: 60 }],
      });
      expect(configResult.success).toBe(true);

      if (configResult.success) {
        await agent.install(configResult.data);
        expect(mockSandbox.arun).toHaveBeenCalled();
      }
    });
  });

  describe('run', () => {
    it('should throw error if not installed', async () => {
      const mockSandbox = createMockSandbox();
      const agent = new DefaultAgent(mockSandbox);

      await expect(agent.run('test prompt')).rejects.toThrow('Agent is not installed');
    });

    it('should throw error if runCmd is not set', async () => {
      const mockSandbox = createMockSandbox();
      const agent = new DefaultAgent(mockSandbox);

      const configResult = RockAgentConfigSchema.safeParse({
        agentSession: 'test-session',
      });
      expect(configResult.success).toBe(true);

      if (configResult.success) {
        await agent.install(configResult.data);
        await expect(agent.run('test prompt')).rejects.toThrow('runCmd is not configured');
      }
    });
  });
});
