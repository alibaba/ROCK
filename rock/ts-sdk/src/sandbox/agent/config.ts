/**
 * Agent configuration schemas
 */

import { z } from 'zod';
import { readFileSync, existsSync } from 'fs';
import { randomUUID } from 'crypto';
import YAML from 'yaml';
import { envVars } from '../../env_vars.js';
import type { ModelServiceConfig } from '../model_service/base.js';

/**
 * Base agent configuration schema
 */
export const AgentConfigSchema = z.object({
  agentType: z.string(),
  version: z.string().default('default'),
});

export type AgentConfig = z.infer<typeof AgentConfigSchema>;

/**
 * Configuration for a command execution with timeout control
 */
export const AgentBashCommandSchema = z.object({
  command: z.string(),
  timeoutSeconds: z.number().int().positive().default(300),
});

export type AgentBashCommand = z.infer<typeof AgentBashCommandSchema>;

/**
 * Default agent configuration schema
 */
export const DefaultAgentConfigSchema = z.object({
  agentType: z.string(),
  version: z.string().default('default'),

  // Session management
  agentSession: z.string().default('default-agent-session'),

  // Startup/shutdown commands
  preInitBashCmdList: z.array(AgentBashCommandSchema).default(
    envVars.ROCK_AGENT_PRE_INIT_BASH_CMD_LIST.map((cmd) => ({
      command: cmd.command,
      timeoutSeconds: cmd.timeoutSeconds || 300,
    }))
  ),
  postInitBashCmdList: z.array(AgentBashCommandSchema).default([]),

  // Environment variables for the session
  sessionEnvs: z.record(z.string()).default({}),

  // Optional ModelService configuration
  modelServiceConfig: z.custom<ModelServiceConfig>().nullable().default(null),
});

export type DefaultAgentConfig = z.infer<typeof DefaultAgentConfigSchema>;

/**
 * RockAgent configuration schema with validation.
 *
 * runtimeEnvConfig defaults to a Python runtime environment config, matching
 * Python's Field(default_factory=PythonRuntimeEnvConfig).
 */
export const RockAgentConfigSchema = z
  .object({
    agentType: z.string().default('default'),
    agentName: z.string().default(() => randomUUID().replace(/-/g, '')),
    version: z.string().default('default'),

    agentInstalledDir: z.string().default('/tmp/installed_agent'),
    instanceId: z.string().default(() => `instance-id-${randomUUID().replace(/-/g, '')}`),

    projectPath: z.string().nullable().default(null),
    useDeployWorkingDirAsFallback: z.boolean().default(true),

    agentSession: z.string().default(() => `agent-session-${randomUUID().replace(/-/g, '')}`),

    env: z.record(z.string()).default({}),

    preInitCmds: z.array(AgentBashCommandSchema).default(
      envVars.ROCK_AGENT_PRE_INIT_BASH_CMD_LIST.map((cmd) => ({
        command: cmd.command,
        timeoutSeconds: cmd.timeoutSeconds || 300,
      }))
    ),
    postInitCmds: z.array(AgentBashCommandSchema).default([]),

    agentInstallTimeout: z.number().int().positive().default(600),
    agentRunTimeout: z.number().int().positive().default(1800),
    agentRunCheckInterval: z.number().int().positive().default(30),

    workingDir: z.string().nullable().default(null),
    runCmd: z.string().nullable().default(null),
    skipWrapRunCmd: z.boolean().default(false),

    /**
     * Runtime environment configuration for the agent.
     * Defaults to a Python runtime env config, matching Python's
     * Field(default_factory=PythonRuntimeEnvConfig).
     *
     * Must be an object with at least a `type` field.
     */
    runtimeEnvConfig: z.record(z.unknown()).nullable().default(null),
    modelServiceConfig: z.custom<ModelServiceConfig>().nullable().default(null),
  })
  .refine((data) => data.agentRunCheckInterval < data.agentRunTimeout, {
    message: 'agentRunCheckInterval must be less than agentRunTimeout',
  });

export type RockAgentConfig = z.infer<typeof RockAgentConfigSchema>;

/**
 * Load RockAgentConfig from a YAML file path.
 *
 * Supports .yaml and .yml files. Throws on missing file, invalid format,
 * or schema validation failure.
 *
 * @param filePath - Path to the YAML config file
 * @returns Parsed and validated RockAgentConfig
 */
export function loadRockAgentConfigFromYaml(filePath: string): RockAgentConfig {
  if (!existsSync(filePath)) {
    throw new Error(`Agent config file not found: ${filePath}`);
  }

  const ext = filePath.split('.').pop()?.toLowerCase();
  if (ext !== 'yaml' && ext !== 'yml') {
    throw new Error(`Unsupported config file format: .${ext}. Only .yaml/.yml is supported.`);
  }

  const raw = readFileSync(filePath, 'utf-8');
  const configDict = YAML.parse(raw);
  return RockAgentConfigSchema.parse(configDict);
}