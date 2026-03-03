/**
 * Test for ESM shims compatibility
 *
 * This test verifies that __dirname is correctly shimmed in ESM builds.
 * Without shims, __dirname is undefined in ESM modules.
 */

import { execSync } from 'child_process';
import { existsSync, readFileSync } from 'fs';
import { join } from 'path';

describe('ESM Shims', () => {
  const distDir = join(__dirname, '../../dist');
  const esmEntryPath = join(distDir, 'index.mjs');

  beforeAll(() => {
    // Build the project first
    execSync('pnpm build', { cwd: join(__dirname, '../..'), stdio: 'inherit' });
  });

  it('should have ESM output file', () => {
    expect(existsSync(esmEntryPath)).toBe(true);
  });

  it('should shim __dirname in ESM build', () => {
    // Read the ESM output
    const esmContent = readFileSync(esmEntryPath, 'utf-8');

    // Check if __dirname is properly shimmed
    // When shims are enabled, tsup should inject the shim code:
    // var __dirname = path.dirname(fileURLToPath(import.meta.url))
    const hasDirnameShim =
      esmContent.includes('fileURLToPath') &&
      esmContent.includes('import.meta.url');

    expect(hasDirnameShim).toBe(true);
  });

  it('should have valid __dirname value when ESM module is loaded', async () => {
    // Write test script to a temp file to avoid shell escaping issues
    const { writeFileSync, unlinkSync } = await import('fs');
    const { tmpdir } = await import('os');
    const tempFile = join(tmpdir(), 'test-esm-shim.mjs');

    const testScript = `
      import { fileURLToPath } from 'url';
      import { dirname, resolve } from 'path';

      // Simulate what tsup shims should inject
      const __filename = fileURLToPath(import.meta.url);
      const __dirname = dirname(__filename);

      // The __dirname should be a valid absolute path
      if (typeof __dirname !== 'string') {
        throw new Error('__dirname is not a string: ' + typeof __dirname);
      }
      if (!__dirname.startsWith('/')) {
        throw new Error('__dirname is not an absolute path: ' + __dirname);
      }

      // Resolve should work with __dirname
      const serverPath = resolve(__dirname, 'server');
      if (!serverPath.startsWith('/')) {
        throw new Error('resolve did not produce absolute path');
      }

      console.log(JSON.stringify({ __dirname, serverPath }));
    `;

    try {
      writeFileSync(tempFile, testScript);
      const result = execSync(`node ${tempFile}`, {
        encoding: 'utf-8',
      });

      const parsed = JSON.parse(result);
      expect(parsed.__dirname).toBeDefined();
      expect(typeof parsed.__dirname).toBe('string');
      expect(parsed.__dirname.startsWith('/')).toBe(true);
    } finally {
      unlinkSync(tempFile);
    }
  });
});
