import { readFileSync } from 'fs';
import { join } from 'path';
import { VERSION } from './index.js';

describe('VERSION', () => {
  it('should match package.json version', () => {
    const packageJsonPath = join(__dirname, '..', 'package.json');
    const packageJson = JSON.parse(readFileSync(packageJsonPath, 'utf-8'));

    expect(VERSION).toBe(packageJson.version);
  });
});
