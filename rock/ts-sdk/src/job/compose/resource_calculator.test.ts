/**
 * Tests for job/compose/resource_calculator.ts
 */

import { coerceCpu, coerceMemoryBytes, calcComposeSandboxResources } from './resource_calculator';
import { ComposeJobConfig } from '../config_compose';

// Helper to build a minimal ComposeJobConfig for testing
function makeConfig(
  services: Array<{ cpu?: string | number; memory?: string }>
): ComposeJobConfig {
  return {
    job_name: 'test-job',
    labels: {},
    environment: {},
    namespace: null,
    experiment_id: null,
    timeout: 7200,
    services: services.map((s, i) => ({
      name: `svc-${i}`,
      image: 'test:latest',
      command: null,
      args: null,
      script: null,
      env: {},
      ports: [],
      resources: s.cpu !== undefined || s.memory !== undefined
        ? { cpu: String(s.cpu ?? '1'), memory: s.memory ?? '2Gi' }
        : null,
      privileged: false,
      volume_mounts: [],
      is_main: i === 0,
    })),
    init_containers: [],
    volumes: [],
    oss_artifacts: [],
    network_mode: 'host' as const,
    callback_url: null,
  };
}

// ---------------------------------------------------------------------------
// coerceCpu
// ---------------------------------------------------------------------------
describe('coerceCpu', () => {
  test('handles millicpu format', () => {
    expect(coerceCpu('500m')).toBe(0.5);
    expect(coerceCpu('1000m')).toBe(1.0);
    expect(coerceCpu('2000m')).toBe(2.0);
    expect(coerceCpu('250m')).toBe(0.25);
  });

  test('handles string integer format', () => {
    expect(coerceCpu('1')).toBe(1.0);
    expect(coerceCpu('2')).toBe(2.0);
    expect(coerceCpu('4')).toBe(4.0);
  });

  test('handles string float format', () => {
    expect(coerceCpu('0.5')).toBe(0.5);
    expect(coerceCpu('2.5')).toBe(2.5);
  });

  test('handles numeric values', () => {
    expect(coerceCpu(2)).toBe(2.0);
    expect(coerceCpu(0.5)).toBe(0.5);
    expect(coerceCpu(3.0)).toBe(3.0);
  });
});

// ---------------------------------------------------------------------------
// coerceMemoryBytes
// ---------------------------------------------------------------------------
describe('coerceMemoryBytes', () => {
  test('handles binary units', () => {
    expect(coerceMemoryBytes('1Ki')).toBe(1024);
    expect(coerceMemoryBytes('1Mi')).toBe(1024 * 1024);
    expect(coerceMemoryBytes('1Gi')).toBe(1024 * 1024 * 1024);
  });

  test('handles decimal units', () => {
    expect(coerceMemoryBytes('1K')).toBe(1000);
    expect(coerceMemoryBytes('1M')).toBe(1000 * 1000);
    expect(coerceMemoryBytes('1G')).toBe(1000 * 1000 * 1000);
  });

  test('handles common K8s memory values', () => {
    expect(coerceMemoryBytes('512Mi')).toBe(512 * 1024 * 1024);
    expect(coerceMemoryBytes('2Gi')).toBe(2 * 1024 * 1024 * 1024);
    expect(coerceMemoryBytes('8Gi')).toBe(8 * 1024 * 1024 * 1024);
    expect(coerceMemoryBytes('16Gi')).toBe(16 * 1024 * 1024 * 1024);
  });

  test('handles plain bytes as string', () => {
    expect(coerceMemoryBytes('4096')).toBe(4096);
  });

  test('handles numeric value', () => {
    expect(coerceMemoryBytes(4096)).toBe(4096);
  });

  test('handles lowercase binary units', () => {
    expect(coerceMemoryBytes('1gi')).toBe(1024 * 1024 * 1024);
    expect(coerceMemoryBytes('512mi')).toBe(512 * 1024 * 1024);
  });
});

// ---------------------------------------------------------------------------
// calcComposeSandboxResources
// ---------------------------------------------------------------------------
describe('calcComposeSandboxResources', () => {
  test('calculates for single service', () => {
    const config = makeConfig([
      { cpu: '2', memory: '4Gi' },
    ]);
    const [memory, cpus] = calcComposeSandboxResources(config);
    // 2 CPU + 1 headroom = 3, but min is 2 → 3
    expect(cpus).toBe(3.0);
    // 4Gi + 2Gi headroom = 6Gi, but > 4Gi min
    expect(memory).toBe('6g');
  });

  test('returns minimum for empty services', () => {
    const config = makeConfig([{ cpu: '0.1', memory: '100Mi' }]);
    const [memory, cpus] = calcComposeSandboxResources(config);
    // 0.1 + 1.0 headroom = 1.1, min CPU is 2
    expect(cpus).toBe(2.0);
    // 100Mi + 2Gi headroom = 2.097Gi, min memory is 4Gi
    expect(memory).toBe('4g');
  });

  test('sums resources across multiple services', () => {
    const config = makeConfig([
      { cpu: '1', memory: '2Gi' },
      { cpu: '2', memory: '2Gi' },
    ]);
    const [memory, cpus] = calcComposeSandboxResources(config);
    // CPU: 1 + 2 + 1 (headroom) = 4
    expect(cpus).toBe(4.0);
    // Memory: 2Gi + 2Gi + 2Gi (headroom) = 6Gi
    expect(memory).toBe('6g');
  });

  test('uses defaults for services without resources', () => {
    const config = makeConfig([{}]);  // no resources specified
    const [memory, cpus] = calcComposeSandboxResources(config);
    // Default: 1 CPU + 1 headroom = 2
    expect(cpus).toBe(2.0);
    // Default: 2Gi + 2Gi headroom = 4Gi
    expect(memory).toBe('4g');
  });
});
