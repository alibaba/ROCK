/**
 * Model module — Model client, ModelService, and server utilities.
 */

export * from './client.js';
export { ModelService } from './service.js';
export type { ModelServiceStartOptions } from './service.js';
export {
  ModelServiceConfigSchema,
  type ModelServiceConfig,
  createModelServiceConfig,
  POLLING_INTERVAL_SECONDS,
  REQUEST_TIMEOUT,
  REQUEST_START_MARKER,
  REQUEST_END_MARKER,
  RESPONSE_START_MARKER,
  RESPONSE_END_MARKER,
  SESSION_END_MARKER,
} from './server/config.js';
export {
  TrajectoryRecorder,
  SequentialCursor,
  TrajectoryExhausted,
} from './server/traj.js';
export type { TrajectoryRecordParams } from './server/traj.js';
export {
  parseSseDataChunks,
  completionToChunkDict,
  encodeSseEvent,
  SSE_DONE,
} from './server/sse.js';
export {
  writeTraj,
  MODEL_SERVICE_REQUEST_RT,
  MODEL_SERVICE_REQUEST_COUNT,
} from './server/utils.js';
export {
  FileHandler,
} from './server/file_handler.js';
export {
  createApp,
  configureProxyIntegrations,
  startServer,
  createConfigFromArgs,
} from './server/main.js';
export {
  localRouter,
  initLocalApi,
  proxyRouter,
  ForwardBackend,
  ReplayBackend,
  filterHeaders,
  type CompletionBackend,
} from './server/api/index.js';
export {
  LOG_DIR,
  LOG_FILE,
  TRAJ_FILE,
  getLogFile,
  getTrajFile,
} from './server/config.js';
