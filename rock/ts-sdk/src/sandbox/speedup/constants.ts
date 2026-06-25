/**
 * Script templates and constants for speedup
 *
 * Mirrors Python rock/sdk/sandbox/speedup/constants.py
 * Extracted verbatim from network.ts inline template literals.
 */

/**
 * Build APT speedup configuration script
 *
 * @param params.mirror_base - Mirror URL base (e.g. "http://mirrors.cloud.aliyuncs.com")
 * @returns Complete bash script
 */
export function buildAptScript(params: { mirror_base: string }): string {
  const mirrorUrl = params.mirror_base;
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
 * Build PIP speedup configuration script
 *
 * @param params.pip_index_url - PIP index URL (e.g. "http://mirrors.cloud.aliyuncs.com/pypi/simple/")
 * @param params.pip_trusted_host - Trusted host from the mirror URL
 * @returns Complete bash script
 */
export function buildPipScript(params: { pip_index_url: string; pip_trusted_host: string }): string {
  const indexUrl = params.pip_index_url;
  const trustedHost = params.pip_trusted_host;
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
 * Build GitHub hosts speedup configuration script
 *
 * @param params.hosts_entry - Hosts file entry (e.g. "11.11.11.11 github.com")
 * @returns Complete bash script
 */
export function buildGithubScript(params: { hosts_entry: string }): string {
  const ipAddress = params.hosts_entry.split(' ')[0];
  return `#!/bin/bash
echo ">>> Configuring GitHub hosts for github.com acceleration..."

# Backup original hosts file if not already backed up
if [ ! -f /etc/hosts.backup ]; then
    cp /etc/hosts /etc/hosts.backup
    echo "Hosts file backed up to /etc/hosts.backup"
fi

# Remove existing github.com entry if any
sed -i '/github\\.com$/d' /etc/hosts

# Add new github.com hosts entry
echo "${ipAddress} github.com" | tee -a /etc/hosts

echo ">>> GitHub hosts configuration completed"
echo "Current github.com entry in /etc/hosts:"
grep 'github\\.com$' /etc/hosts || echo "No github.com entry found"
`;
}
