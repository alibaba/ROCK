/**
 * Network - Network management for sandbox
 */

import { initLogger } from '../logger.js';
import type { Observation } from '../types/responses.js';
import type { Sandbox } from './client.js';
import { validateUrl, validateIpAddress, shellQuote } from '../utils/shell.js';

const logger = initLogger('rock.sandbox.network');

/**
 * Speedup type enum
 */
export enum SpeedupType {
  APT = 'apt',
  PIP = 'pip',
  GITHUB = 'github',
}

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

    // Validate input based on type
    let validatedValue: string;
    switch (speedupType) {
      case SpeedupType.APT:
      case SpeedupType.PIP:
        validatedValue = validateUrl(speedupValue);
        break;
      case SpeedupType.GITHUB:
        validatedValue = validateIpAddress(speedupValue);
        break;
      default:
        throw new Error(`Unsupported speedup type: ${speedupType}`);
    }

    // Generate script content
    const scriptContent = this.generateSpeedupScript(speedupType, validatedValue);

    // Execute script using the process module (uploads script file and executes)
    const result = await this.sandbox.getProcess().executeScript({
      scriptContent,
      waitTimeout: timeout,
    });

    return result;
  }

  /**
   * Generate speedup script content based on type
   */
  private generateSpeedupScript(speedupType: SpeedupType, value: string): string {
    switch (speedupType) {
      case SpeedupType.APT:
        return this.buildAptSpeedupScript(value);
      case SpeedupType.PIP:
        return this.buildPipSpeedupScript(value);
      case SpeedupType.GITHUB:
        return this.buildGithubSpeedupScript(value);
      default:
        throw new Error(`Unsupported speedup type: ${speedupType}`);
    }
  }

  /**
   * Build APT speedup script
   * Uses script file approach for safety
   */
  private buildAptSpeedupScript(mirrorUrl: string): string {
    return `#!/bin/bash
detect_system_and_version() {
    if [ -f /etc/debian_version ]; then
        . /etc/os-release
        if [ "$ID" = "ubuntu" ]; then
            echo "ubuntu:$VERSION_CODENAME"
        elif [ "$ID" = "debian" ]; then
            echo "debian:$VERSION_CODENAME"
        else
            echo "unknown:"
        fi
    else
        echo "unknown:"
    fi
}

SYSTEM_INFO=$(detect_system_and_version)
SYSTEM=$(echo "$SYSTEM_INFO" | cut -d: -f1)
CODENAME=$(echo "$SYSTEM_INFO" | cut -d: -f2)
echo "System type: $SYSTEM, Version codename: $CODENAME"

# Backup original sources file
if [ ! -f /etc/apt/sources.list.backup ]; then
    cp /etc/apt/sources.list /etc/apt/sources.list.backup
fi

if [ "$SYSTEM" = "debian" ]; then
    if [ -z "$CODENAME" ]; then
        CODENAME="bookworm"
    fi
    cat > /etc/apt/sources.list <<EOF
deb ${mirrorUrl}/debian/ \${CODENAME} main non-free non-free-firmware contrib
deb ${mirrorUrl}/debian-security/ \${CODENAME}-security main
deb ${mirrorUrl}/debian/ \${CODENAME}-updates main non-free non-free-firmware contrib
EOF
elif [ "$SYSTEM" = "ubuntu" ]; then
    if [ -z "$CODENAME" ]; then
        if [ -f /etc/os-release ]; then
            VERSION_ID=$(grep VERSION_ID /etc/os-release | cut -d'"' -f2)
            case "$VERSION_ID" in
                "24.04") CODENAME="noble" ;;
                "22.04") CODENAME="jammy" ;;
                "20.04") CODENAME="focal" ;;
                *) CODENAME="noble" ;;
            esac
        else
            CODENAME="noble"
        fi
    fi
    cat > /etc/apt/sources.list <<EOF
deb ${mirrorUrl}/ubuntu/ $CODENAME main restricted universe multiverse
deb ${mirrorUrl}/ubuntu/ $CODENAME-security main restricted universe multiverse
deb ${mirrorUrl}/ubuntu/ $CODENAME-updates main restricted universe multiverse
deb ${mirrorUrl}/ubuntu/ $CODENAME-backports main restricted universe multiverse
EOF
fi

# Clean up other source files
rm -rf /etc/apt/sources.list.d

# Clean APT cache and update
apt-get clean
rm -rf /var/lib/apt/lists/*
echo ">>> APT source configuration completed"
`;
  }

  /**
   * Build PIP speedup script
   * Uses script file approach for safety
   */
  private buildPipSpeedupScript(mirrorUrl: string): string {
    const parsed = new URL(mirrorUrl);
    const trustedHost = parsed.host;
    const indexUrl = `${mirrorUrl}/pypi/simple/`;

    return `#!/bin/bash
echo ">>> Configuring pip source..."

# Configure for root user
mkdir -p /root/.pip
cat > /root/.pip/pip.conf <<EOF
[global]
index-url = ${indexUrl}
trusted-host = ${trustedHost}
timeout = 120

[install]
trusted-host = ${trustedHost}
EOF

# Configure for other existing users
for home_dir in /home/*; do
    if [ -d "$home_dir" ]; then
        username=$(basename "$home_dir")
        mkdir -p "$home_dir/.pip"
        cat > "$home_dir/.pip/pip.conf" <<EOF
[global]
index-url = ${indexUrl}
trusted-host = ${trustedHost}
timeout = 120

[install]
trusted-host = ${trustedHost}
EOF
        chown -R "$username:$username" "$home_dir/.pip" 2>/dev/null || true
    fi
done

echo ">>> pip source configuration completed"
`;
  }

  /**
   * Build GitHub speedup script
   * Uses script file approach for safety
   */
  private buildGithubSpeedupScript(ipAddress: string): string {
    return `#!/bin/bash
echo ">>> Configuring GitHub hosts for github.com acceleration..."

# Backup original hosts file if not already backed up
if [ ! -f /etc/hosts.backup ]; then
    cp /etc/hosts /etc/hosts.backup
    echo "Hosts file backed up to /etc/hosts.backup"
fi

# Remove existing github.com entry if any
sed -i '/github\.com$/d' /etc/hosts

# Add new github.com hosts entry
echo "${ipAddress} github.com" | tee -a /etc/hosts

echo ">>> GitHub hosts configuration completed"
echo "Current github.com entry in /etc/hosts:"
grep 'github\.com$' /etc/hosts || echo "No github.com entry found"
`;
  }
}