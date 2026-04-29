/**
 * KmonHostIpResolver - Query hostIp for destroyed sandboxes via Kmon metrics API
 */

import axios from 'axios';
import { initLogger } from '../logger.js';
import { envVars } from '../env_vars.js';
import { sleep } from '../utils/retry.js';

const logger = initLogger('rock.sandbox.kmon');

/**
 * Kmon configuration
 */
export interface KmonConfig {
  /** Kmon API Token (required) */
  token?: string;
  /** Kmon API base URL */
  baseUrl?: string;
  /** Tenant list to search */
  tenants?: string[];
  /** Maximum query days */
  maxQueryDays?: number;
  /** Maximum query range in milliseconds (default: 2 days) */
  maxQueryRangeMs?: number;
}

/**
 * HostIp resolver function type
 */
export type HostIpResolver = (sandboxId: string) => Promise<string>;

/**
 * KmonHostIpResolver - Query hostIp via Kmon metrics API
 * 
 * Features:
 * - Token from parameter or ROCK_KMON_TOKEN environment variable
 * - Tenant fallback: default -> gen_ai
 * - Auto-segment queries (max 2 days per query)
 * - Rate limiting (QPS <= 5, interval >= 200ms)
 */
export class KmonHostIpResolver {
  private config: Required<KmonConfig>;
  private lastRequestTime: number = 0;
  private readonly MIN_REQUEST_INTERVAL = 200; // 1000ms / 5 QPS = 200ms

  constructor(config: KmonConfig = {}) {
    const token = config.token ?? envVars.ROCK_KMON_TOKEN;
    if (!token) {
      throw new Error('ROCK_KMON_TOKEN is required');
    }

    this.config = {
      token,
      baseUrl: config.baseUrl ?? envVars.ROCK_KMON_BASE_URL ?? 'https://kmon-metric.alibaba-inc.com',
      tenants: config.tenants ?? envVars.ROCK_KMON_TENANTS?.split(',') ?? ['default', 'gen_ai'],
      maxQueryDays: config.maxQueryDays ?? 7,
      maxQueryRangeMs: config.maxQueryRangeMs ?? 2 * 24 * 60 * 60 * 1000, // 2 days
    };
  }

  /**
   * Resolve sandboxId to hostIp
   * - Searches through all tenants
   * - Auto-segments time range
   * - Rate limited
   */
  async resolve(sandboxId: string): Promise<string> {
    // Search through each tenant
    for (const tenant of this.config.tenants) {
      const hostIp = await this.queryWithTenant(sandboxId, tenant);
      if (hostIp) {
        logger.info(`Found hostIp ${hostIp} for sandbox ${sandboxId} in tenant ${tenant}`);
        return hostIp;
      }
    }
    throw new Error(`Cannot find hostIp for sandbox ${sandboxId} in any tenant`);
  }

  /**
   * Query a specific tenant with auto time segmentation
   */
  private async queryWithTenant(sandboxId: string, tenant: string): Promise<string | null> {
    const now = Date.now();
    const maxAgeMs = this.config.maxQueryDays * 24 * 60 * 60 * 1000;
    const startTime = now - maxAgeMs;

    // Segment query, max 2 days per segment
    let currentStart = startTime;
    while (currentStart < now) {
      const currentEnd = Math.min(currentStart + this.config.maxQueryRangeMs, now);

      // Rate limit
      await this.waitForRateLimit();

      const hostIp = await this.queryRange(sandboxId, tenant, currentStart, currentEnd);
      if (hostIp) {
        return hostIp;
      }

      currentStart = currentEnd;
    }

    return null;
  }

  /**
   * Wait for rate limit
   */
  private async waitForRateLimit(): Promise<void> {
    const now = Date.now();
    const elapsed = now - this.lastRequestTime;
    if (elapsed < this.MIN_REQUEST_INTERVAL) {
      await sleep(this.MIN_REQUEST_INTERVAL - elapsed);
    }
    this.lastRequestTime = Date.now();
  }

  /**
   * Query a specific time range
   */
  private async queryRange(
    sandboxId: string,
    tenant: string,
    start: number,
    end: number
  ): Promise<string | null> {
    const url = `${this.config.baseUrl}/api/query?token=${this.config.token}&tenant=${tenant}`;

    try {
      const response = await axios.post(url, {
        start,
        end,
        queries: [{
          metric: 'xrl_gateway.system.cpu',
          tags: { ip: '*', sandbox_id: sandboxId },
          downsample: 'avg',
          aggregator: 'avg',
          granularity: '1m',
        }],
      }, {
        headers: { 'Content-Type': 'application/json' },
      });

      // Response is an array directly, not { results: [...] }
      const data = response.data as Array<{ tags?: { ip?: string } }>;

      // Extract hostIp from response
      if (Array.isArray(data) && data.length > 0 && data[0]?.tags?.ip) {
        return data[0].tags.ip;
      }

      return null;
    } catch (e) {
      logger.warn(`Kmon query error: ${e}`);
      return null;
    }
  }
}
