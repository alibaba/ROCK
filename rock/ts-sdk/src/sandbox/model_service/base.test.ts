/**
 * ModelService tests
 */

import {
  ModelServiceConfig,
  ModelServiceConfigSchema,
  ModelService,
} from './base.js';

describe('ModelServiceConfig', () => {
  describe('ModelServiceConfigSchema', () => {
    it('should parse valid config with default values', () => {
      const result = ModelServiceConfigSchema.safeParse({});
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.enabled).toBe(false);
        expect(result.data.type).toBe('local');
        expect(result.data.installTimeout).toBe(300);
        expect(result.data.loggingPath).toBe('/data/logs');
        expect(result.data.loggingFileName).toBe('model_service.log');
      }
    });

    it('should parse config with enabled=true', () => {
      const result = ModelServiceConfigSchema.safeParse({ enabled: true });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.enabled).toBe(true);
      }
    });

    it('should parse config with custom type', () => {
      const result = ModelServiceConfigSchema.safeParse({ type: 'proxy' });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.type).toBe('proxy');
      }
    });

    it('should parse config with custom commands', () => {
      const result = ModelServiceConfigSchema.safeParse({
        startCmd: 'custom start command',
        stopCmd: 'custom stop command',
        watchAgentCmd: 'custom watch command',
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.startCmd).toBe('custom start command');
        expect(result.data.stopCmd).toBe('custom stop command');
        expect(result.data.watchAgentCmd).toBe('custom watch command');
      }
    });

    it('should reject invalid installTimeout (non-positive)', () => {
      const result = ModelServiceConfigSchema.safeParse({ installTimeout: 0 });
      expect(result.success).toBe(false);
    });

    it('should reject invalid installTimeout (negative)', () => {
      const result = ModelServiceConfigSchema.safeParse({ installTimeout: -1 });
      expect(result.success).toBe(false);
    });
  });

  describe('ModelServiceConfig type', () => {
    it('should be inferred from schema', () => {
      const config = ModelServiceConfigSchema.parse({});
      expect(config.type).toBe('local');
    });
  });
});

describe('ModelService', () => {
  describe('constructor', () => {
    it('should create instance with default config', () => {
      const config = ModelServiceConfigSchema.parse({ enabled: true });
      
      // Create a minimal mock sandbox
      const mockSandbox = {
        sandboxId: 'test-sandbox-id',
        runtimeEnvs: {},
        createSession: jest.fn().mockResolvedValue({}),
        arun: jest.fn().mockResolvedValue({
          output: '',
          exitCode: 0,
          failureReason: '',
          expectString: '',
        }),
      };

      const service = new ModelService(
        mockSandbox as unknown as ConstructorParameters<typeof ModelService>[0],
        config
      );

      expect(service.isInstalled).toBe(false);
      expect(service.isStarted).toBe(false);
    });
  });

  describe('start', () => {
    it('should throw error if not installed', async () => {
      const config = ModelServiceConfigSchema.parse({ enabled: true });
      
      const mockSandbox = {
        sandboxId: 'test-sandbox-id',
        runtimeEnvs: {},
        createSession: jest.fn().mockResolvedValue({}),
        arun: jest.fn().mockResolvedValue({
          output: '',
          exitCode: 0,
          failureReason: '',
          expectString: '',
        }),
      };

      const service = new ModelService(
        mockSandbox as unknown as ConstructorParameters<typeof ModelService>[0],
        config
      );

      await expect(service.start()).rejects.toThrow('not been installed');
    });
  });

  describe('watchAgent', () => {
    it('should throw error if not started', async () => {
      const config = ModelServiceConfigSchema.parse({ enabled: true });
      
      const mockSandbox = {
        sandboxId: 'test-sandbox-id',
        runtimeEnvs: {},
        createSession: jest.fn().mockResolvedValue({}),
        arun: jest.fn().mockResolvedValue({
          output: '',
          exitCode: 0,
          failureReason: '',
          expectString: '',
        }),
      };

      const service = new ModelService(
        mockSandbox as unknown as ConstructorParameters<typeof ModelService>[0],
        config
      );

      await expect(service.watchAgent('12345')).rejects.toThrow('not started');
    });
  });

  describe('stop', () => {
    it('should skip stop if not started', async () => {
      const config = ModelServiceConfigSchema.parse({ enabled: true });
      
      const mockSandbox = {
        sandboxId: 'test-sandbox-id',
        runtimeEnvs: {},
        createSession: jest.fn().mockResolvedValue({}),
        arun: jest.fn().mockResolvedValue({
          output: '',
          exitCode: 0,
          failureReason: '',
          expectString: '',
        }),
      };

      const service = new ModelService(
        mockSandbox as unknown as ConstructorParameters<typeof ModelService>[0],
        config
      );

      // Should not throw, just skip
      await service.stop();
      expect(service.isStarted).toBe(false);
    });
  });
});
