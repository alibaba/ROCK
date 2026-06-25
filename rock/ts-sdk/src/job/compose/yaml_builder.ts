/**
 * Compose YAML builder — generate docker-compose.yaml from ComposeJobConfig.
 *
 * Matches Python rock.sdk.job.compose.yaml_builder.
 */

import yaml from 'yaml';
import type { ComposeJobConfig, ServiceConfig } from '../config_compose';

// ---------------------------------------------------------------------------
// Main builder
// ---------------------------------------------------------------------------

/**
 * Build docker-compose.yaml content and per-service script files.
 *
 * Returns:
 *   [compose_yaml_text, scripts_dict] where scripts_dict maps
 *   filename -> script content for services with script fields.
 */
export function buildComposeYaml(config: ComposeJobConfig): [string, Record<string, string>] {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const composeDoc: Record<string, any> = {
    version: '3.8',
    services: {},
    volumes: {},
    networks: { default: { driver: 'bridge' } },
  };

  const scripts: Record<string, string> = {};

  for (const service of config.services) {
    const svcDef = _buildServiceDef(service, config);
    composeDoc['services'][service.name] = svcDef;

    if (service.script) {
      const scriptFilename = `${service.name}.sh`;
      scripts[scriptFilename] = service.script;
      svcDef['entrypoint'] = ['bash', `/tmp/run/${scriptFilename}`];
    }
  }

  for (const vol of config.volumes) {
    if (vol.host_path) {
      composeDoc['volumes'][vol.name] = { driver: 'local' };
    } else {
      composeDoc['volumes'][vol.name] = {};
    }
  }

  let yamlText = yaml.stringify(composeDoc, { sortMapEntries: false });
  yamlText = _escapeDollar(yamlText);
  return [yamlText, scripts];
}

// ---------------------------------------------------------------------------
// Service definition
// ---------------------------------------------------------------------------

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function _buildServiceDef(service: ServiceConfig, config: ComposeJobConfig): Record<string, any> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const svc: Record<string, any> = { image: service.image };

  if (config.network_mode === 'host') {
    svc['network_mode'] = 'host';
  }

  const volumes = _buildVolumes(service, config);
  if (volumes.length > 0) {
    svc['volumes'] = volumes;
  }

  const env = _buildEnv(service, config);
  if (Object.keys(env).length > 0) {
    svc['environment'] = env;
  }

  if (service.command) {
    svc['command'] = service.command;
  }

  if (service.privileged) {
    svc['privileged'] = true;
  }

  return svc;
}

// ---------------------------------------------------------------------------
// Volumes
// ---------------------------------------------------------------------------

function _buildVolumes(service: ServiceConfig, config: ComposeJobConfig): string[] {
  const mounts = [
    '/tmp/shared:/tmp/shared',
    '/tmp/output:/tmp/output',
    '/workspace/scripts:/tmp/run:ro',
    '/var/run/docker.sock:/var/run/docker.sock',
  ];

  for (const vm of service.volume_mounts) {
    const matchingVol = config.volumes.find((v) => v.name === vm.name);
    let mountStr: string;
    if (matchingVol && matchingVol.host_path) {
      mountStr = `${matchingVol.host_path}:${vm.mount_path}`;
    } else {
      mountStr = `${vm.name}:${vm.mount_path}`;
    }
    if (vm.read_only) {
      mountStr += ':ro';
    }
    mounts.push(mountStr);
  }

  return mounts;
}

// ---------------------------------------------------------------------------
// Environment
// ---------------------------------------------------------------------------

function _buildEnv(
  service: ServiceConfig,
  config: ComposeJobConfig
): Record<string, string> {
  const env: Record<string, string> = {};
  env['JOB_ID'] = config.job_name ?? '';

  // Merge global environment from the sandbox config
  const globalEnv = (config.environment as Record<string, unknown>)?.env;
  if (globalEnv && typeof globalEnv === 'object') {
    Object.assign(env, globalEnv as Record<string, string>);
  }

  // Service-level env overrides
  Object.assign(env, service.env);

  return env;
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function _escapeDollar(text: string): string {
  return text.replace(/\$/g, '$$');
}
