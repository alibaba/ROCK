import {
  AgentConfigSchema,
  AgentBashCommandSchema,
  DefaultAgentConfigSchema,
  RockAgentConfigSchema,
} from './config.js';

describe('AgentConfig', () => {
  describe('AgentConfigSchema', () => {
    it('should parse valid config with required fields', () => {
      const result = AgentConfigSchema.safeParse({
        agentType: 'default',
        version: '1.0.0',
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.agentType).toBe('default');
        expect(result.data.version).toBe('1.0.0');
      }
    });

    it('should use default version when not specified', () => {
      const result = AgentConfigSchema.safeParse({
        agentType: 'default',
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.version).toBe('default');
      }
    });

    it('should reject config without agentType', () => {
      const result = AgentConfigSchema.safeParse({
        version: '1.0.0',
      });
      expect(result.success).toBe(false);
    });
  });
});

describe('AgentBashCommand', () => {
  describe('AgentBashCommandSchema', () => {
    it('should parse valid command', () => {
      const result = AgentBashCommandSchema.safeParse({
        command: 'echo hello',
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.command).toBe('echo hello');
        expect(result.data.timeoutSeconds).toBe(300);
      }
    });

    it('should parse command with custom timeout', () => {
      const result = AgentBashCommandSchema.safeParse({
        command: 'long-running-command',
        timeoutSeconds: 600,
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.timeoutSeconds).toBe(600);
      }
    });

    it('should reject config without command', () => {
      const result = AgentBashCommandSchema.safeParse({
        timeoutSeconds: 300,
      });
      expect(result.success).toBe(false);
    });

    it('should reject non-positive timeout', () => {
      const result = AgentBashCommandSchema.safeParse({
        command: 'echo hello',
        timeoutSeconds: 0,
      });
      expect(result.success).toBe(false);
    });
  });
});

describe('DefaultAgentConfig', () => {
  describe('DefaultAgentConfigSchema', () => {
    it('should parse valid config with defaults', () => {
      const result = DefaultAgentConfigSchema.safeParse({
        agentType: 'default',
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.agentType).toBe('default');
        expect(result.data.agentSession).toBe('default-agent-session');
        expect(result.data.sessionEnvs).toEqual({});
        expect(result.data.preInitBashCmdList).toBeInstanceOf(Array);
        expect(result.data.postInitBashCmdList).toEqual([]);
        expect(result.data.modelServiceConfig).toBeNull();
      }
    });

    it('should parse config with sessionEnvs', () => {
      const result = DefaultAgentConfigSchema.safeParse({
        agentType: 'default',
        sessionEnvs: { NODE_ENV: 'development' },
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.sessionEnvs).toEqual({ NODE_ENV: 'development' });
      }
    });
  });
});

describe('RockAgentConfig', () => {
  describe('RockAgentConfigSchema', () => {
    it('should parse valid config with defaults', () => {
      const result = RockAgentConfigSchema.safeParse({});
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.agentType).toBe('default');
        expect(result.data.version).toBe('default');
        expect(result.data.agentInstalledDir).toBe('/tmp/installed_agent');
        expect(result.data.projectPath).toBeNull();
        expect(result.data.useDeployWorkingDirAsFallback).toBe(true);
        expect(result.data.env).toEqual({});
        expect(result.data.agentInstallTimeout).toBe(600);
        expect(result.data.agentRunTimeout).toBe(1800);
        expect(result.data.agentRunCheckInterval).toBe(30);
        expect(result.data.workingDir).toBeNull();
        expect(result.data.runCmd).toBeNull();
        expect(result.data.skipWrapRunCmd).toBe(false);
      }
    });

    it('should parse config with custom values', () => {
      const result = RockAgentConfigSchema.safeParse({
        agentType: 'custom-agent',
        version: '2.0.0',
        agentInstalledDir: '/custom/agent/dir',
        projectPath: '/path/to/project',
        env: { API_KEY: 'secret' },
        agentInstallTimeout: 1200,
        agentRunTimeout: 3600,
        agentRunCheckInterval: 60,
        workingDir: '/local/workdir',
        runCmd: 'node agent.js --prompt {prompt}',
        skipWrapRunCmd: true,
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.agentType).toBe('custom-agent');
        expect(result.data.version).toBe('2.0.0');
        expect(result.data.agentInstalledDir).toBe('/custom/agent/dir');
        expect(result.data.projectPath).toBe('/path/to/project');
        expect(result.data.env).toEqual({ API_KEY: 'secret' });
        expect(result.data.agentInstallTimeout).toBe(1200);
        expect(result.data.agentRunTimeout).toBe(3600);
        expect(result.data.agentRunCheckInterval).toBe(60);
        expect(result.data.workingDir).toBe('/local/workdir');
        expect(result.data.runCmd).toBe('node agent.js --prompt {prompt}');
        expect(result.data.skipWrapRunCmd).toBe(true);
      }
    });

    it('should reject when agentRunCheckInterval >= agentRunTimeout', () => {
      const result = RockAgentConfigSchema.safeParse({
        agentRunTimeout: 30,
        agentRunCheckInterval: 60,
      });
      expect(result.success).toBe(false);
      if (!result.success) {
        expect(result.error.issues.some((issue) => issue.message.includes('must be less than'))).toBe(true);
      }
    });

    it('should reject non-positive agentInstallTimeout', () => {
      const result = RockAgentConfigSchema.safeParse({
        agentInstallTimeout: 0,
      });
      expect(result.success).toBe(false);
    });

    it('should reject non-positive agentRunTimeout', () => {
      const result = RockAgentConfigSchema.safeParse({
        agentRunTimeout: -1,
      });
      expect(result.success).toBe(false);
    });

    it('should generate unique agentSession if not specified', () => {
      const result1 = RockAgentConfigSchema.safeParse({});
      const result2 = RockAgentConfigSchema.safeParse({});
      expect(result1.success).toBe(true);
      expect(result2.success).toBe(true);
      if (result1.success && result2.success) {
        expect(result1.data.agentSession).toMatch(/^agent-session-/);
        expect(result2.data.agentSession).toMatch(/^agent-session-/);
        expect(result1.data.agentSession).not.toBe(result2.data.agentSession);
      }
    });

    it('should generate unique instanceId if not specified', () => {
      const result1 = RockAgentConfigSchema.safeParse({});
      const result2 = RockAgentConfigSchema.safeParse({});
      expect(result1.success).toBe(true);
      expect(result2.success).toBe(true);
      if (result1.success && result2.success) {
        expect(result1.data.instanceId).toMatch(/^instance-id-/);
        expect(result2.data.instanceId).toMatch(/^instance-id-/);
        expect(result1.data.instanceId).not.toBe(result2.data.instanceId);
      }
    });

    it('should generate unique agentName if not specified', () => {
      const result1 = RockAgentConfigSchema.safeParse({});
      const result2 = RockAgentConfigSchema.safeParse({});
      expect(result1.success).toBe(true);
      expect(result2.success).toBe(true);
      if (result1.success && result2.success) {
        expect(result1.data.agentName).not.toBe(result2.data.agentName);
      }
    });
  });
});
