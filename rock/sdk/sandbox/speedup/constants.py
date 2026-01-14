"""Script templates and constants for speedup."""

# APT speedup configuration script template (unified)
setup_apt_source_template = """#!/bin/bash

detect_system_and_version() {{
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
}}

setup_apt_source() {{
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
deb {mirror_base}/debian/ ${{CODENAME}} main non-free non-free-firmware contrib
deb {mirror_base}/debian-security/ ${{CODENAME}}-security main
deb {mirror_base}/debian/ ${{CODENAME}}-updates main non-free non-free-firmware contrib
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
deb {mirror_base}/ubuntu/ $CODENAME main restricted universe multiverse
deb {mirror_base}/ubuntu/ $CODENAME-security main restricted universe multiverse
deb {mirror_base}/ubuntu/ $CODENAME-updates main restricted universe multiverse
deb {mirror_base}/ubuntu/ $CODENAME-backports main restricted universe multiverse
EOF
    fi

    # Clean up other source files
    rm -rf /etc/apt/sources.list.d

    # Set APT configuration for faster downloads
    cat > /etc/apt/apt.conf.d/99speedup <<EOF
Acquire::http::Timeout "30";
Acquire::ftp::Timeout "30";
Acquire::Retries "3";
APT::Acquire::Retries "3";
APT::Get::Assume-Yes "true";
APT::Install-Recommends "false";
APT::Install-Suggests "false";
EOF

    # Clean APT cache and update
    apt-get clean
    rm -rf /var/lib/apt/lists/*
    echo ">>> APT source configuration completed"
}}

setup_apt_source
"""

# PIP speedup configuration script template
setup_pip_source_template = """#!/bin/bash

setup_pip_source() {{
    echo ">>> Configuring pip source..."

    # Configure for root user
    mkdir -p /root/.pip
    cat > /root/.pip/pip.conf <<EOF
[global]
index-url = {pip_index_url}
trusted-host = {pip_trusted_host}
timeout = 120

[install]
trusted-host = {pip_trusted_host}
EOF

    # Configure for other existing users
    for home_dir in /home/*; do
        if [ -d "$home_dir" ]; then
            username=$(basename "$home_dir")
            mkdir -p "$home_dir/.pip"
            cat > "$home_dir/.pip/pip.conf" <<EOF
[global]
index-url = {pip_index_url}
trusted-host = {pip_trusted_host}
timeout = 120

[install]
trusted-host = {pip_trusted_host}
EOF
            chown -R "$username:$username" "$home_dir/.pip" 2>/dev/null || true
        fi
    done

    echo ">>> pip source configuration completed"
}}

setup_pip_source
"""

# GitHub hosts speedup configuration script template (github.com only)
setup_github_hosts_template = """#!/bin/bash

setup_github_hosts() {{
    echo ">>> Configuring GitHub hosts for github.com acceleration..."

    # Backup original hosts file if not already backed up
    if [ ! -f /etc/hosts.backup ]; then
        cp /etc/hosts /etc/hosts.backup
        echo "Hosts file backed up to /etc/hosts.backup"
    fi

    # Remove existing github.com entry if any
    sed -i '/github\\.com$/d' /etc/hosts

    # Add new github.com hosts entry
    echo "{hosts_entry}" | tee -a /etc/hosts

    echo ">>> GitHub hosts configuration completed"
    echo "Current github.com entry in /etc/hosts:"
    grep 'github\\.com$' /etc/hosts || echo "No github.com entry found"
}}

setup_github_hosts
"""
