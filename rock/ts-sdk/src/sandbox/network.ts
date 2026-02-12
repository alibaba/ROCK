/**
 * Network - Network management for sandbox
 */

import { initLogger } from '../logger.js';
import type { Observation } from '../types/responses.js';
import type { Sandbox } from './client.js';
import { SpeedupType } from './types.js';

const logger = initLogger('rock.sandbox.network');

/**
 * Network management for sandbox
 */
export class Network {
  private sandbox: Sandbox;

  constructor(sandbox: Sandbox) {
    this.sandbox = sandbox;
  }

  /**
   * Configure acceleration for package managers or network resources
   *
   * @param speedupType - Type of speedup configuration
   * @param speedupValue - Speedup value (mirror URL or IP address)
   * @param timeout - Execution timeout in seconds
   * @returns Observation with execution result
   */
  async speedup(
    speedupType: SpeedupType,
    speedupValue: string,
    timeout: number = 300
  ): Promise<Observation> {
    const sandboxId = this.sandbox.getSandboxId();
    logger.info(
      `[${sandboxId}] Configuring ${speedupType} speedup: ${speedupValue}`
    );

    let command: string;

    switch (speedupType) {
      case SpeedupType.APT:
        command = this.buildAptSpeedupCommand(speedupValue);
        break;
      case SpeedupType.PIP:
        command = this.buildPipSpeedupCommand(speedupValue);
        break;
      case SpeedupType.GITHUB:
        command = this.buildGithubSpeedupCommand(speedupValue);
        break;
      default:
        throw new Error(`Unsupported speedup type: ${speedupType}`);
    }

    const result = await this.sandbox.arun(command, {
      mode: 'nohup',
      waitTimeout: timeout,
    });

    return result;
  }

  private buildAptSpeedupCommand(mirrorUrl: string): string {
    return `cat > /etc/apt/sources.list << 'EOF'
deb ${mirrorUrl} $(lsb_release -cs) main restricted universe multiverse
deb ${mirrorUrl} $(lsb_release -cs)-updates main restricted universe multiverse
deb ${mirrorUrl} $(lsb_release -cs)-backports main restricted universe multiverse
deb ${mirrorUrl} $(lsb_release -cs)-security main restricted universe multiverse
EOF
apt-get update`;
  }

  private buildPipSpeedupCommand(mirrorUrl: string): string {
    const safeUrl = mirrorUrl.replace(/'/g, "'\\''");
    return `mkdir -p ~/.pip && cat > ~/.pip/pip.conf << 'EOF'
[global]
index-url = ${safeUrl}
trusted-host = $(echo ${safeUrl} | sed 's|https\\?://||' | cut -d'/' -f1)
EOF`;
  }

  private buildGithubSpeedupCommand(ipAddress: string): string {
    return `echo "${ipAddress} github.com" >> /etc/hosts`;
  }
}
