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

# Check unzip
if ! command -v unzip >/dev/null 2>&1; then
    echo "ERROR: unzip is not available. Please install unzip first." >&2
    exit 1
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
