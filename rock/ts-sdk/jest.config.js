/** @type {import('ts-jest').JestConfigWithTsJest} */
const baseConfig = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  moduleFileExtensions: ['ts', 'tsx', 'js', 'jsx', 'json', 'node'],
  moduleNameMapper: {
    '^(\\.{1,2}/.*)\\.js$': '$1',
  },
  transform: {
    '^.+\\.tsx?$': [
      'ts-jest',
      {
        useESM: true,
        tsconfig: {
          module: 'ESNext',
          moduleResolution: 'bundler',
          experimentalDecorators: true,
        },
      },
    ],
  },
  extensionsToTreatAsEsm: ['.ts'],
};

module.exports = {
  projects: [
    // Unit tests - fast, isolated tests in src/
    {
      ...baseConfig,
      displayName: 'unit',
      roots: ['<rootDir>/src'],
      testMatch: ['**/*.test.ts'],
      collectCoverageFrom: [
        'src/**/*.ts',
        '!src/**/*.d.ts',
        '!src/**/index.ts',
      ],
      coverageDirectory: 'coverage',
    },
    // Integration tests - tests that require external services
    {
      ...baseConfig,
      displayName: 'integration',
      roots: ['<rootDir>/tests/integration'],
      testMatch: ['**/*.test.ts'],
    },
  ],
};