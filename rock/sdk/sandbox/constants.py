"""Script templates and constants for sandbox operations."""

# Ensure ossutil is installed in the sandbox.
# - Checks wget/curl availability (fails fast if neither is present)
# - Checks unzip availability (fails fast if missing)
# - Skips installation if ossutil is already in PATH
ENSURE_OSSUTIL_SCRIPT = """#!/bin/bash
set -e

# Check downloader
if command -v wget >/dev/null 2>&1; then
    DOWNLOADER="wget"
elif command -v curl >/dev/null 2>&1; then
    DOWNLOADER="curl"
else
    echo "ERROR: neither wget nor curl is available. Please install one first." >&2
    exit 1
fi

# Check unzip — try to install if missing
if ! command -v unzip >/dev/null 2>&1; then
    echo "unzip not found, attempting to install..."
    apt-get install -y -q unzip 2>/dev/null || yum install -y -q unzip 2>/dev/null || true
    if ! command -v unzip >/dev/null 2>&1; then
        echo "ERROR: unzip is not available and could not be installed." >&2
        exit 1
    fi
fi

# Skip if already installed
if command -v ossutil >/dev/null 2>&1; then
    echo "ossutil already installed, skipping."
    exit 0
fi

# Download
cd /tmp
if [ "$DOWNLOADER" = "wget" ]; then
    wget -q https://gosspublic.alicdn.com/ossutil/v2/2.2.1/ossutil-2.2.1-linux-amd64.zip -O /tmp/ossutil.zip
else
    curl -sL -o /tmp/ossutil.zip https://gosspublic.alicdn.com/ossutil/v2/2.2.1/ossutil-2.2.1-linux-amd64.zip
fi

# Extract and install
unzip -o -q ossutil.zip
chmod 755 /tmp/ossutil-2.2.1-linux-amd64/ossutil
mkdir -p /usr/local/bin
mv /tmp/ossutil-2.2.1-linux-amd64/ossutil /usr/local/bin/

# Cleanup
rm -rf /tmp/ossutil.zip /tmp/ossutil-2.2.1-linux-amd64

# Verify
ossutil version
"""


# Start dockerd inside a builder sandbox (DinD). Idempotent; if dockerd is
# already running, just wait for it to become responsive.
DOCKERD_SCRIPT = r"""#!/bin/bash
set -e
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

if command -v dockerd &>/dev/null; then
    if ! pgrep -x dockerd &>/dev/null; then
        echo "Starting dockerd..."
        nohup dockerd &>/var/log/dockerd.log &
    fi
    for i in $(seq 1 60); do
        if docker info &>/dev/null; then echo "DOCKERD_OK"; break; fi
        sleep 1
        if [ "$i" -eq 60 ]; then
            echo "DOCKERD_FAIL"
            cat /var/log/dockerd.log 2>/dev/null | tail -50
            exit 1
        fi
    done
fi
"""


# `docker build` inside the builder sandbox. Format placeholders:
#   image_name, content_hash, registry, registry_username, registry_password,
#   force_build, build_arg_flags, context_path.
#
# Logs in to the registry (so a private-registry manifest probe works), then
# runs a builder-side cache check via `docker manifest inspect`. This is a
# second layer on top of the SDK-side registry preflight in image_builder.py:
# the SDK can't always reach the registry (e.g. user laptop outside the VPC
# where the registry lives), so we re-check from the builder's network. When
# the image already exists we emit CACHE_HIT and skip the actual build.
BUILD_SCRIPT_TEMPLATE = r"""#!/bin/bash
set -e

IMAGE_NAME={image_name}
CONTENT_HASH={content_hash}
REGISTRY={registry}
REG_USER={registry_username}
REG_PASS={registry_password}
FORCE_BUILD={force_build}

# ── Registry login (so manifest inspect works on private registries) ──
if [ -n "$REG_USER" ] && [ -n "$REG_PASS" ]; then
    echo "$REG_PASS" | docker login "$REGISTRY" -u "$REG_USER" --password-stdin
fi

# ── Cache check from builder's network ──
if [ "$FORCE_BUILD" != "true" ]; then
    if docker manifest inspect "$IMAGE_NAME" > /dev/null 2>&1; then
        echo "CACHE_HIT"
        echo "BUILD_OK"
        exit 0
    fi
fi

# ── Build ──
echo "Building image $IMAGE_NAME..."
docker build {build_arg_flags} --label rock.content_hash="$CONTENT_HASH" -t "$IMAGE_NAME" {context_path}
echo "BUILD_OK"
"""


# `docker login` + `docker push` inside the builder sandbox. Format
# placeholders: image_name, registry, registry_username, registry_password.
PUSH_SCRIPT_TEMPLATE = r"""#!/bin/bash
set -e

IMAGE_NAME={image_name}
REGISTRY={registry}
REG_USER={registry_username}
REG_PASS={registry_password}

# ── Registry login ──
if [ -n "$REG_USER" ] && [ -n "$REG_PASS" ]; then
    echo "$REG_PASS" | docker login "$REGISTRY" -u "$REG_USER" --password-stdin
else
    echo "No registry credentials, skipping login"
fi

# ── Docker push ──
echo "Pushing image $IMAGE_NAME..."
docker push "$IMAGE_NAME"
echo "PUSH_OK"
"""
