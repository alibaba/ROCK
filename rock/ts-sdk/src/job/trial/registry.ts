/**
 * Trial registry — maps JobConfig types to their AbstractTrial implementations.
 *
 * Matches Python rock.sdk.job.trial.registry.
 *
 * In Python, the registry maps ``type(JobConfig)`` → ``type(AbstractTrial)``.
 * In TypeScript, configs are plain objects (Zod-inferred), not classes, so we
 * use a registry key object (symbol) that each config module exports.
 */

import type { AbstractTrial } from './abstract';

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------

/** Registry key — each config module exports a unique symbol. */
type RegistryKey = symbol;

/** Map from registry key to trial implementation constructor. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const _TRIAL_REGISTRY = new Map<RegistryKey, new (...args: any[]) => AbstractTrial>();

/**
 * Register a Config → Trial mapping.
 *
 * Args:
 *   key: A unique symbol exported by the config module (e.g., BASH_JOB_CONFIG_KEY)
 *   trialType: The AbstractTrial subclass constructor
 */
export function registerTrial(
  key: RegistryKey,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  trialType: new (...args: any[]) => AbstractTrial
): void {
  _TRIAL_REGISTRY.set(key, trialType);
}

/**
 * Lookup the trial constructor for a given registry key.
 *
 * @param key The registry key for the config type
 * @returns The trial constructor
 * @throws TypeError if no trial class has been registered for this key
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function _lookupTrial(key: RegistryKey): new (...args: any[]) => AbstractTrial {
  const trialCtor = _TRIAL_REGISTRY.get(key);
  if (!trialCtor) {
    const supported = Array.from(_TRIAL_REGISTRY.keys())
      .map((k) => k.description ?? String(k))
      .join(', ');
    throw new TypeError(
      `No trial registered for ${key.description ?? 'unknown'}. Supported: [${supported}]`
    );
  }
  return trialCtor;
}

/**
 * Create a Trial instance for the given config, using its internal registry key.
 *
 * The config object must have a ``_registryKey`` symbol property that was set
 * during parsing (via Zod transform or by the config factory function).
 */
export function createTrial(config: Record<string, unknown>): AbstractTrial {
  const key = (config as any)['_registryKey'] as RegistryKey | undefined;
  if (!key) {
    throw new TypeError(
      `Config object has no _registryKey. Ensure it was created via a config factory (e.g., createBashJobConfig).`
    );
  }
  const trialCtor = _lookupTrial(key);
  return new trialCtor(config);
}

/**
 * Assign a registry key to a config object (used by config factory functions).
 */

export function _assignRegistryKey(config: Record<string, unknown>, key: RegistryKey): void {
  (config as any)['_registryKey'] = key;
}
