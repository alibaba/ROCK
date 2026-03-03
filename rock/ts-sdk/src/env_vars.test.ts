/**
 * Tests for environment variables configuration
 */

import { envVars } from './env_vars.js';

describe('envVars', () => {
  describe('Python install command URLs', () => {
    test('V31114 install command URL should contain releases/download/ path', () => {
      const cmd = envVars.ROCK_RTENV_PYTHON_V31114_INSTALL_CMD;
      expect(cmd).toContain('releases/download/');
    });

    test('V31212 install command URL should contain releases/download/ path', () => {
      const cmd = envVars.ROCK_RTENV_PYTHON_V31212_INSTALL_CMD;
      expect(cmd).toContain('releases/download/');
    });
  });
});
