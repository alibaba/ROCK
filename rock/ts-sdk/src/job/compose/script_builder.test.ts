/**
 * Tests for job/compose/script_builder.ts — runner.sh generation
 */

import { buildRunnerScript } from './script_builder';
import { ComposeJobConfig } from '../config_compose';

function makeConfig(overrides?: Partial<ComposeJobConfig>): ComposeJobConfig {
  return {
    job_name: 'test-compose-job',
    labels: {},
    environment: { env: { GLOBAL_KEY: 'global_val' } },
    namespace: null,
    experiment_id: null,
    timeout: 7200,
    services: [
      {
        name: 'main',
        image: 'myapp:latest',
        command: null,
        args: null,
        script: null,
        env: {},
        ports: [],
        resources: null,
        privileged: false,
        volume_mounts: [],
        is_main: true,
      },
    ],
    init_containers: [],
    volumes: [],
    oss_artifacts: [],
    network_mode: 'host',
    callback_url: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// buildRunnerScript
// ---------------------------------------------------------------------------
describe('buildRunnerScript', () => {
  test('generates a non-empty script', () => {
    const config = makeConfig();
    const script = buildRunnerScript(config);
    expect(typeof script).toBe('string');
    expect(script.length).toBeGreaterThan(100);
  });

  test('starts with shebang', () => {
    const config = makeConfig();
    const script = buildRunnerScript(config);
    expect(script.startsWith('#!/bin/bash')).toBe(true);
  });

  test('includes job_id from config', () => {
    const config = makeConfig({ job_name: 'my-custom-job' });
    const script = buildRunnerScript(config);
    expect(script).toContain('my-custom-job');
  });

  test('includes timeout from config', () => {
    const config = makeConfig({ timeout: 3600 });
    const script = buildRunnerScript(config);
    expect(script).toContain('3600');
  });

  test('includes dockerd startup section', () => {
    const config = makeConfig();
    const script = buildRunnerScript(config);
    expect(script).toContain('dockerd');
    expect(script).toContain('DOCKERD_TIMEOUT');
    expect(script).toContain('exit 90');
  });

  test('includes docker compose up section', () => {
    const config = makeConfig();
    const script = buildRunnerScript(config);
    expect(script).toContain('docker compose');
    expect(script).toContain('exit 91');
  });

  test('includes main container wait section', () => {
    const config = makeConfig();
    const script = buildRunnerScript(config);
    expect(script).toContain('docker wait');
    expect(script).toContain('compose-main-1');
  });

  test('includes trap cleanup', () => {
    const config = makeConfig();
    const script = buildRunnerScript(config);
    expect(script).toContain('trap cleanup EXIT');
    expect(script).toContain('docker compose -f');
    expect(script).toContain('down');
  });

  test('includes init container section when provided', () => {
    const config = makeConfig({
      init_containers: [{
        name: 'setup',
        image: 'alpine:latest',
        command: ['echo', 'init'],
        args: null,
        script: null,
        volume_mounts: [],
      }],
    });
    const script = buildRunnerScript(config);
    expect(script).toContain('init');
    expect(script).toContain('alpine:latest');
    expect(script).toContain('exit 92');
  });

  test('includes OSS artifact download section when provided', () => {
    const config = makeConfig({
      oss_artifacts: [{
        name: 'model',
        oss_key: 'models/v1.tar.gz',
        target_path: '/workspace',
        archive: true,
      }],
    });
    const script = buildRunnerScript(config);
    expect(script).toContain('ossutil');
    expect(script).toContain('model');
    expect(script).toContain('models/v1.tar.gz');
  });

  test('includes callback URL when provided', () => {
    const config = makeConfig({ callback_url: 'http://hooks.example.com' });
    const script = buildRunnerScript(config);
    expect(script).toContain('http://hooks.example.com');
  });

  test('handles init container with script', () => {
    const config = makeConfig({
      init_containers: [{
        name: 'db-migrate',
        image: 'migrator:latest',
        command: null,
        args: null,
        script: '#!/bin/bash\necho "migrating..."',
        volume_mounts: [],
      }],
    });
    const script = buildRunnerScript(config);
    expect(script).toContain('migrating');
    expect(script).toContain('db-migrate');
  });

  test('no exit 92 when no init containers', () => {
    const config = makeConfig();
    const script = buildRunnerScript(config);
    expect(script).not.toContain('exit 92');
  });
});
