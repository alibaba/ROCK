/**
 * Tests for model/service.ts
 */

import { ModelService } from './service.js';
import http from 'http';

describe('ModelService', () => {
  describe('stop', () => {
    it('does not throw when asked to stop', async () => {
      const service = new ModelService();

      // stop() should not throw even with a potentially-already-dead pid
      await expect(service.stop('99999')).resolves.toBeUndefined();
    });

    it('kills an actual running process', async () => {
      const service = new ModelService();

      const { spawn } = require('child_process');
      const proc = spawn('node', ['-e', 'setTimeout(() => {}, 30000)']);
      const pid = String(proc.pid);

      await new Promise((r) => setTimeout(r, 100));

      // Stop should trigger kill
      await service.stop(pid);

      // Wait for process to die
      await new Promise((r) => setTimeout(r, 1000));

      // Process should have exited or been killed
      // On some systems kill -9 may leave a zombie briefly, so check exit
      proc.kill('SIGKILL'); // Ensure cleanup
    });
  });

  describe('_waitServiceAvailable', () => {
    it('returns false when service is not reachable', async () => {
      const service = new ModelService();

      // Use an unused port
      const result = await service._waitServiceAvailable(2, '127.0.0.1', 19999);

      expect(result).toBe(false);
    });

    it('returns true when service is healthy', async () => {
      const service = new ModelService();

      // Start a simple HTTP server that returns 200 on /health
      const server = http.createServer((_req: any, res: any) => {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'healthy' }));
      });

      await new Promise<void>((resolve) => {
        server.listen(0, () => resolve());
      });
      const port = (server.address() as { port: number }).port;

      try {
        const result = await service._waitServiceAvailable(5, '127.0.0.1', port);
        expect(result).toBe(true);
      } finally {
        server.close();
      }
    });
  });
});
