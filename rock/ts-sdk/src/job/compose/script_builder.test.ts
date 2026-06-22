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

  test('guards each init container command exactly once without placeholders', () => {
    const config = makeConfig({
      init_containers: [
        {
          name: 'prepare',
          image: 'alpine:3.19',
          command: ['echo', 'prepare'],
          args: null,
          script: null,
          volume_mounts: [],
        },
        {
          name: 'migrate',
          image: 'busybox:1.36',
          command: ['echo', 'migrate'],
          args: null,
          script: null,
          volume_mounts: [],
        },
      ],
    });

    const script = buildRunnerScript(config);

    expect(script).not.toContain('LAST_COMMAND');
    expect(script.match(/docker run --rm --network host/g)).toHaveLength(2);
    expect(script).toContain("if ! docker run --rm --network host -v '/tmp/shared:/tmp/shared' -v '/tmp/output:/tmp/output' 'alpine:3.19' 'echo' 'prepare'; then");
    expect(script).toContain("if ! docker run --rm --network host -v '/tmp/shared:/tmp/shared' -v '/tmp/output:/tmp/output' 'busybox:1.36' 'echo' 'migrate'; then");
  });

  test('shell-quotes config values embedded in runner commands', () => {
    const config = makeConfig({
      job_name: 'job$(touch /tmp/job-pwn)',
      callback_url: 'http://hooks.example.com/$(touch /tmp/callback-pwn)',
      services: [
        {
          name: 'main$(touch /tmp/service-pwn)',
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
      oss_artifacts: [{
        name: 'model$(touch /tmp/name-pwn)',
        oss_key: 'models/$(touch /tmp/key-pwn).tar.gz',
        target_path: '/workspace/$(touch /tmp/target-pwn)',
        archive: true,
      }],
      init_containers: [{
        name: 'setup$(touch /tmp/init-name-pwn)',
        image: 'alpine:$(touch /tmp/image-pwn)',
        command: ['echo', '$(touch /tmp/cmd-pwn)'],
        args: null,
        script: null,
        volume_mounts: [{
          name: '/tmp/shared$(touch /tmp/volume-pwn)',
          mount_path: '/mnt/shared$(touch /tmp/mount-pwn)',
          read_only: false,
        }],
      }],
    });

    const script = buildRunnerScript(config);

    expect(script).toContain("JOB_ID='job$(touch /tmp/job-pwn)'");
    expect(script).not.toContain('JOB_ID="job$(touch /tmp/job-pwn)"');
    expect(script).toContain("CALLBACK_URL='http://hooks.example.com/$(touch /tmp/callback-pwn)'");
    expect(script).not.toContain('CALLBACK_URL="http://hooks.example.com/$(touch /tmp/callback-pwn)"');
    expect(script).toContain("MAIN_CONTAINER='compose-main$(touch /tmp/service-pwn)-1'");
    expect(script).toContain("mkdir -p '/workspace/$(touch /tmp/target-pwn)'");
    expect(script).toContain('"oss://$OSS_BUCKET/"' + "'models/$(touch /tmp/key-pwn).tar.gz'");
    expect(script).toContain("'/tmp/model$(touch /tmp/name-pwn).tar.gz'");
    expect(script).toContain("'/tmp/shared$(touch /tmp/volume-pwn):/mnt/shared$(touch /tmp/mount-pwn)'");
    expect(script).toContain("'alpine:$(touch /tmp/image-pwn)' 'echo' '$(touch /tmp/cmd-pwn)'");
  });

  test('no exit 92 when no init containers', () => {
    const config = makeConfig();
    const script = buildRunnerScript(config);
    expect(script).not.toContain('exit 92');
  });
});
