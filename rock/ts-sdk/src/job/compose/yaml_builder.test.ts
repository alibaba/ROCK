/**
 * Tests for job/compose/yaml_builder.ts — docker-compose.yaml generation
 */

import { buildComposeYaml } from './yaml_builder';
import { ComposeJobConfig } from '../config_compose';

// Helper to build a minimal ComposeJobConfig for testing
function makeConfig(overrides?: Partial<ComposeJobConfig>): ComposeJobConfig {
  return {
    job_name: 'test-job',
    labels: {},
    environment: { env: { GLOBAL_VAR: 'global_value' } },
    namespace: null,
    experiment_id: null,
    timeout: 7200,
    services: [
      {
        name: 'main',
        image: 'myapp:latest',
        command: ['python', 'app.py'],
        args: null,
        script: null,
        env: { APP_MODE: 'production' },
        ports: [8080],
        resources: { cpu: '2', memory: '4Gi' },
        privileged: false,
        volume_mounts: [{ name: 'data', mount_path: '/data', read_only: false }],
        is_main: true,
      },
    ],
    init_containers: [],
    volumes: [{ name: 'data', host_path: '/mnt/data' }],
    oss_artifacts: [],
    network_mode: 'host',
    callback_url: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// buildComposeYaml
// ---------------------------------------------------------------------------
describe('buildComposeYaml', () => {
  test('returns yaml string and scripts dict', () => {
    const config = makeConfig();
    const [yaml, scripts] = buildComposeYaml(config);
    expect(typeof yaml).toBe('string');
    expect(yaml.length).toBeGreaterThan(0);
    expect(typeof scripts).toBe('object');
  });

  test('generates valid YAML with version and services', () => {
    const config = makeConfig();
    const [yaml] = buildComposeYaml(config);

    expect(yaml).toContain('version');
    expect(yaml).toContain('3.8');
    expect(yaml).toContain('services');
    expect(yaml).toContain('main');
  });

  test('includes service image and command', () => {
    const config = makeConfig({
      services: [{
        name: 'main', image: 'myapp:latest',
        command: ['python', 'app.py'],
        args: null, script: null, env: {}, ports: [],
        resources: null, privileged: false, volume_mounts: [], is_main: true,
      }],
    });
    const [yaml] = buildComposeYaml(config);
    expect(yaml).toContain('myapp:latest');
    expect(yaml).toContain('python');
    expect(yaml).toContain('app.py');
  });

  test('includes network_mode host', () => {
    const config = makeConfig({ network_mode: 'host' });
    const [yaml] = buildComposeYaml(config);
    expect(yaml).toContain('network_mode');
    expect(yaml).toContain('host');
  });

  test('includes volumes section', () => {
    const config = makeConfig({
      volumes: [{ name: 'data', host_path: '/mnt/data' }],
    });
    const [yaml] = buildComposeYaml(config);
    expect(yaml).toContain('volumes');
  });

  test('includes shared mounts', () => {
    const config = makeConfig();
    const [yaml] = buildComposeYaml(config);
    // Shared mounts: /tmp/shared, /tmp/output, /workspace/scripts, docker.sock
    expect(yaml).toContain('/tmp/shared');
    expect(yaml).toContain('/tmp/output');
  });

  test('handles bridge network mode', () => {
    const config = makeConfig({ network_mode: 'bridge' });
    const [yaml] = buildComposeYaml(config);
    // bridge mode should NOT have network_mode: host
    expect(yaml).not.toContain('network_mode');
  });

  test('includes environment variables', () => {
    const config = makeConfig({
      services: [{
        name: 'main', image: 'img:latest',
        command: null, args: null, script: null,
        env: { FOO: 'bar', KEY: 'val' },
        ports: [], resources: null, privileged: false,
        volume_mounts: [], is_main: true,
      }],
    });
    const [yaml] = buildComposeYaml(config);
    // Environment should appear in YAML
    expect(yaml).toContain('FOO');
    expect(yaml).toContain('bar');
    expect(yaml).toContain('KEY');
    expect(yaml).toContain('val');
  });

  test('generates script entry for services with script field', () => {
    const config = makeConfig({
      services: [{
        name: 'main', image: 'img:latest',
        command: null, args: null,
        script: '#!/bin/bash\necho hello',
        env: {}, ports: [], resources: null,
        privileged: false, volume_mounts: [],
        is_main: true,
      }],
    });
    const [yaml, scripts] = buildComposeYaml(config);
    // Script should go in scripts dict, service should have entrypoint
    expect(scripts['main.sh']).toBe('#!/bin/bash\necho hello');
    expect(yaml).toContain('entrypoint');
    expect(yaml).toContain('/tmp/run/main.sh');
  });

  test('handles multiple services', () => {
    const config = makeConfig({
      services: [
        {
          name: 'main', image: 'app:latest', ports: [],
          command: null, args: null, script: null, env: {},
          resources: null, privileged: false, volume_mounts: [],
          is_main: true,
        },
        {
          name: 'db', image: 'postgres:16', ports: [5432],
          command: null, args: null, script: null,
          env: { POSTGRES_PASSWORD: 'secret' },
          resources: { cpu: '1', memory: '2Gi' },
          privileged: false, volume_mounts: [{ name: 'pgdata', mount_path: '/var/lib/postgresql/data', read_only: false }],
          is_main: false,
        },
      ],
    });
    const [yaml, scripts] = buildComposeYaml(config);
    expect(yaml).toContain('app:latest');
    expect(yaml).toContain('postgres:16');
    expect(scripts).toEqual({});  // No scripts for these services
  });
});
