/**
 * Model Service server entry point.
 *
 * Creates an Express app for the Model Service and starts the HTTP server.
 * Supports two modes:
 * - 'local': file-based communication (local API router)
 * - 'proxy': upstream LLM proxy (proxy API router)
 *
 * Mirrors rock/sdk/model/server/main.py.
 */

import express from 'express';
import { initLogger } from '../../logger.js';
import { ModelServiceConfig, ModelServiceConfigSchema } from './config.js';
import { localRouter, initLocalApi } from './api/local.js';
import { proxyRouter } from './api/proxy.js';
import { SequentialCursor, TrajectoryRecorder } from './traj.js';
import { getTrajFile } from './config.js';

const logger = initLogger('rock.model.server.main');

// ---------------------------------------------------------------------------
// App factory
// ---------------------------------------------------------------------------

/**
 * Create a new Express app with the given config.
 */
export function createApp(config: ModelServiceConfig): express.Express {
  const app = express();

  // Store config on app state for access by routers
  app.locals.model_service_config = config;

  // JSON body parser
  app.use(express.json());

  // Health check endpoint
  app.get('/health', (_req, res) => {
    res.json({ status: 'healthy' });
  });

  // Global error handler
  app.use(
    (
      err: Error,
      _req: express.Request,
      res: express.Response,
      _next: express.NextFunction,
    ) => {
      logger.error(`Unhandled exception: ${err.message}`, err);
      res.status(500).json({
        error: {
          message: err.message,
          type: 'internal_error',
          code: 'internal_error',
        },
      });
    },
  );

  return app;
}

// ---------------------------------------------------------------------------
// Proxy integration configurator
// ---------------------------------------------------------------------------

/**
 * Attach the appropriate backend to app.locals.backend.
 *
 * - Replay mode (replay_file set): ReplayBackend wrapping a SequentialCursor.
 *   No recorder — replaying back into the source file would corrupt it.
 * - Forward mode (default): ForwardBackend with a TrajectoryRecorder
 *   writing to recording_file (or TRAJ_FILE if unset).
 */
export function configureProxyIntegrations(
  app: express.Express,
  config: ModelServiceConfig,
): void {
  const { ForwardBackend, ReplayBackend } = require('./api/proxy.js');

  if (config.replay_file) {
    const cursor = SequentialCursor.load(config.replay_file);
    app.locals.backend = new ReplayBackend(cursor);
    logger.info(`replay backend attached, replay_file=${config.replay_file}`);
    return;
  }

  const recordingPath = config.recording_file ?? getTrajFile();
  const recorder = new TrajectoryRecorder(recordingPath);
  app.locals.backend = new ForwardBackend(config, recorder);
  logger.info(`forward backend attached, recording_file=${recordingPath}`);
}

// ---------------------------------------------------------------------------
// Server startup
// ---------------------------------------------------------------------------

/**
 * Start the Model Service server.
 */
export async function startServer(
  modelServiceType: string,
  config: ModelServiceConfig,
): Promise<void> {
  const app = createApp(config);

  if (modelServiceType === 'local') {
    await initLocalApi();
    app.use('/', localRouter);
  } else {
    configureProxyIntegrations(app, config);
    app.use('/', proxyRouter);
  }

  logger.info(
    `Starting Model Service on ${config.host}:${config.port}, type: ${modelServiceType}`,
  );

  return new Promise((resolve) => {
    app.listen(config.port, config.host, () => {
      resolve();
    });
  });
}

// ---------------------------------------------------------------------------
// CLI entry
// ---------------------------------------------------------------------------

/**
 * Create a ModelServiceConfig from command-line arguments.
 *
 * Loads from YAML file if --config-file is specified, then overrides with
 * individual CLI flags (matching Python's create_config_from_args).
 */
export function createConfigFromArgs(args: Record<string, unknown>): ModelServiceConfig {
  // Base config from file or defaults
  let config: ModelServiceConfig;
  if (args.config_file && typeof args.config_file === 'string') {
    // Load from YAML file
    const fs = require('fs');
    const yaml = require('yaml');

    try {
      const content = fs.readFileSync(args.config_file, 'utf-8');
      const data = yaml.parse(content) ?? {};
      config = ModelServiceConfigSchema.parse(data);
      logger.info(`Model Service Config loaded from: ${args.config_file}`);
    } catch (e) {
      logger.error(`Failed to load config from ${args.config_file}: ${e}`);
      throw e;
    }
  } else {
    config = ModelServiceConfigSchema.parse({});
  }

  // CLI overrides
  if (typeof args.host === 'string' && args.host) {
    config.host = args.host;
  }
  if (typeof args.port === 'number') {
    config.port = args.port;
  }
  if (typeof args.proxy_base_url === 'string' && args.proxy_base_url) {
    config.proxy_base_url = args.proxy_base_url;
  }
  if (typeof args.retryable_status_codes === 'string' && args.retryable_status_codes) {
    config.retryable_status_codes = args.retryable_status_codes
      .split(',')
      .map((c: string) => parseInt(c.trim(), 10));
  }
  if (typeof args.request_timeout === 'number') {
    config.request_timeout = args.request_timeout;
  }
  if (typeof args.recording_file === 'string' && args.recording_file) {
    config.recording_file = args.recording_file;
  }
  if (typeof args.replay_file === 'string' && args.replay_file) {
    config.replay_file = args.replay_file;
  }

  return config;
}
