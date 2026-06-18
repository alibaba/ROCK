/**
 * Resource calculator — sum CPU and memory across all compose services.
 *
 * Supports K8s-style resource units:
 *   CPU:    "500m" → 0.5, "2" → 2.0, 2 → 2.0
 *   Memory: "512Mi" → 512MiB, "2Gi" → 2GiB, "4096" → 4096 bytes
 *
 * Matches Python rock.sdk.job.compose.resource_calculator.
 */

import type { ComposeJobConfig } from '../config_compose';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const CPU_HEADROOM = 1.0;
const MEMORY_HEADROOM_BYTES = 2 * 1024 * 1024 * 1024; // 2 GiB

const BINARY_UNITS: Record<string, number> = {
  Ki: 1024,
  Mi: 1024 ** 2,
  Gi: 1024 ** 3,
  Ti: 1024 ** 4,
};
const DECIMAL_UNITS: Record<string, number> = {
  K: 1000,
  M: 1000 ** 2,
  G: 1000 ** 3,
  T: 1000 ** 4,
};

// ---------------------------------------------------------------------------
// CPU coercion
// ---------------------------------------------------------------------------

/**
 * Convert CPU value to float cores. "500m" → 0.5, "2" → 2.0.
 */
export function coerceCpu(value: string | number): number {
  if (typeof value === 'number') return value;
  const s = String(value).trim();
  if (s.endsWith('m')) {
    return parseFloat(s.slice(0, -1)) / 1000.0;
  }
  return parseFloat(s);
}

// ---------------------------------------------------------------------------
// Memory coercion
// ---------------------------------------------------------------------------

/**
 * Convert memory value to bytes.
 * Supports binary (Ki/Mi/Gi/Ti) and decimal (K/M/G/T) suffixes, plus
 * lowercase variants.
 */
export function coerceMemoryBytes(value: string | number): number {
  if (typeof value === 'number') return Math.trunc(value);

  const s = String(value).trim();

  // Try binary suffixes
  for (const [suffix, multiplier] of Object.entries(BINARY_UNITS)) {
    if (s.endsWith(suffix)) {
      return Math.trunc(parseFloat(s.slice(0, -suffix.length)) * multiplier);
    }
  }

  // Try decimal suffixes
  for (const [suffix, multiplier] of Object.entries(DECIMAL_UNITS)) {
    if (s.endsWith(suffix)) {
      return Math.trunc(parseFloat(s.slice(0, -suffix.length)) * multiplier);
    }
  }

  // Try lowercase variants
  const sLower = s.toLowerCase();
  for (const [suffix, multiplier] of Object.entries({ ...BINARY_UNITS, ...DECIMAL_UNITS })) {
    if (sLower.endsWith(suffix.toLowerCase())) {
      return Math.trunc(parseFloat(s.slice(0, -suffix.length)) * multiplier);
    }
  }

  // Plain bytes
  return Math.trunc(parseFloat(s));
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function _formatMemory(totalBytes: number): string {
  const gib = totalBytes / (1024 ** 3);
  if (gib >= 1 && gib === Math.trunc(gib)) {
    return `${Math.trunc(gib)}g`;
  }
  const mib = totalBytes / (1024 ** 2);
  if (mib >= 1 && mib === Math.trunc(mib)) {
    return `${Math.trunc(mib)}m`;
  }
  return `${Math.trunc(totalBytes)}`;
}

// ---------------------------------------------------------------------------
// Main calculator
// ---------------------------------------------------------------------------

/**
 * Calculate total sandbox resources needed for all compose services.
 *
 * Returns [memory_string, cpu_count] with headroom for dockerd and runner.sh.
 * Minimum: 4 GiB memory, 2 CPUs.
 */
export function calcComposeSandboxResources(config: ComposeJobConfig): [string, number] {
  let totalCpu = 0.0;
  let totalMemBytes = 0;

  for (const service of config.services) {
    if (service.resources) {
      totalCpu += coerceCpu(service.resources.cpu);
      totalMemBytes += coerceMemoryBytes(service.resources.memory);
    } else {
      totalCpu += 1.0;
      totalMemBytes += 2 * 1024 ** 3; // 2 GiB default per service
    }
  }

  totalCpu += CPU_HEADROOM;
  totalMemBytes += MEMORY_HEADROOM_BYTES;

  if (totalCpu < 2) totalCpu = 2.0;
  if (totalMemBytes < 4 * 1024 ** 3) totalMemBytes = 4 * 1024 ** 3;

  return [_formatMemory(totalMemBytes), totalCpu];
}
