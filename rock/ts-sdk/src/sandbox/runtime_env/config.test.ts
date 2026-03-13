import { RuntimeEnvConfig, RuntimeEnvConfigSchema } from './config.js';

describe('RuntimeEnvConfig', () => {
  describe('RuntimeEnvConfigSchema', () => {
    it('should parse valid config with required type field', () => {
      const result = RuntimeEnvConfigSchema.safeParse({ type: 'python' });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.type).toBe('python');
      }
    });

    it('should use default values for optional fields', () => {
      const result = RuntimeEnvConfigSchema.safeParse({ type: 'python' });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.version).toBe('default');
        expect(result.data.env).toEqual({});
        expect(result.data.installTimeout).toBe(600);
        expect(result.data.customInstallCmd).toBeNull();
        expect(result.data.extraSymlinkDir).toBeNull();
        expect(result.data.extraSymlinkExecutables).toEqual([]);
      }
    });

    it('should parse config with all fields specified', () => {
      const result = RuntimeEnvConfigSchema.safeParse({
        type: 'node',
        version: '22.18.0',
        env: { NODE_ENV: 'production' },
        installTimeout: 1200,
        customInstallCmd: 'npm install',
        extraSymlinkDir: '/usr/local/bin',
        extraSymlinkExecutables: ['node', 'npm'],
      });
      expect(result.success).toBe(true);
      if (result.success) {
        expect(result.data.type).toBe('node');
        expect(result.data.version).toBe('22.18.0');
        expect(result.data.env).toEqual({ NODE_ENV: 'production' });
        expect(result.data.installTimeout).toBe(1200);
        expect(result.data.customInstallCmd).toBe('npm install');
        expect(result.data.extraSymlinkDir).toBe('/usr/local/bin');
        expect(result.data.extraSymlinkExecutables).toEqual(['node', 'npm']);
      }
    });

    it('should reject config without type field', () => {
      const result = RuntimeEnvConfigSchema.safeParse({ version: '3.11' });
      expect(result.success).toBe(false);
    });

    it('should reject invalid installTimeout (non-positive)', () => {
      const result = RuntimeEnvConfigSchema.safeParse({
        type: 'python',
        installTimeout: 0,
      });
      expect(result.success).toBe(false);
    });

    it('should reject invalid installTimeout (negative)', () => {
      const result = RuntimeEnvConfigSchema.safeParse({
        type: 'python',
        installTimeout: -1,
      });
      expect(result.success).toBe(false);
    });
  });

  describe('RuntimeEnvConfig type', () => {
    it('should be inferred from schema', () => {
      const config: RuntimeEnvConfig = {
        type: 'python',
        version: 'default',
        env: {},
        installTimeout: 600,
        customInstallCmd: null,
        extraSymlinkDir: null,
        extraSymlinkExecutables: [],
      };
      expect(config.type).toBe('python');
    });
  });
});
