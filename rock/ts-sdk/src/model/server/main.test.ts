/**
 * Tests for model/server/main.ts
 */

import { createApp } from './main.js';
import { ModelServiceConfigSchema, type ModelServiceConfig } from './config.js';

describe('createApp', () => {
  it('creates an Express app with health endpoint', () => {
    const config = ModelServiceConfigSchema.parse({});
    const app = createApp(config);

    // Verify app is an Express app with standard methods
    expect(app).toBeDefined();
    expect(typeof app.get).toBe('function');
    expect(typeof app.use).toBe('function');
  });

  it('sets model_service_config on app state', () => {
    const config = ModelServiceConfigSchema.parse({ host: '127.0.0.1', port: 9999 });
    const app = createApp(config);

    expect(app).toBeDefined();
    // The app state should carry the config
    expect(app.locals.model_service_config).toBeDefined();
    expect(app.locals.model_service_config.host).toBe('127.0.0.1');
    expect(app.locals.model_service_config.port).toBe(9999);
  });
});
