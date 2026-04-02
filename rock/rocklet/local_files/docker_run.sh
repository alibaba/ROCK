#!/bin/bash
set -o errexit

port=$1

# Log directory: use /tmp if the user is not root, in case of permission issues
if [ "$(whoami)" != "root" ]; then
    LOG_DIR="/tmp/data/logs"
else
    LOG_DIR="/data/logs"
fi

is_musl() {
    if ldd --version 2>&1 | grep -q musl; then
        echo "true"
    elif [ -e /lib/ld-musl-x86_64.so.1 ] || [ -e /lib/ld-musl-aarch64.so.1 ] && [ ! -f /usr/glibc-compat/lib/libc.so.6 ]; then
        echo "true"
    else
        echo "false"
    fi
}

is_nix() {
    if [ -d /nix/store ]; then
        echo "true"
    else
        echo "false"
    fi
}

# Kata DinD: set up loop device and mount disk image for Docker storage
setup_kata_dind() {
    mkdir -p /var/lib/docker
    for i in $(seq 0 7); do
        mknod -m 660 /dev/loop$i b 7 $i 2>/dev/null || true
    done
    mount -o loop /docker-disk.img /var/lib/docker
    mount -o remount,rw /sys/fs/cgroup
    mount -o remount,rw /proc/sys
}

if [ "${ROCK_KATA_RUNTIME}" = "true" ]; then
    echo "Kata runtime detected, setting up DinD disk..."
    setup_kata_dind
fi

# Run rocklet
if [ "$(is_nix)" = "true" ]; then
    # NixOS
    ln -sf $(ls -d /nix/store/*glibc*/lib 2>/dev/null | head -1) /lib
    ln -sf $(ls -d /nix/store/*glibc*/lib64 2>/dev/null | head -1) /lib64
    mkdir -p /bin
    ln -sf $(ls -d /nix/store/*bash*/bin/bash 2>/dev/null | head -1) /bin/bash
    GCC_LIB=$(ls -d /nix/store/*gcc*lib/lib 2>/dev/null | head -1)
    ZLIB_LIB=$(ls -d /nix/store/*zlib*/lib 2>/dev/null | head -1)
    NIX_LIBS=""
    [ -n "$GCC_LIB" ] && NIX_LIBS="${GCC_LIB}:"
    [ -n "$ZLIB_LIB" ] && NIX_LIBS="${NIX_LIBS}${ZLIB_LIB}:"
    [ -n "$NIX_LIBS" ] && export LD_LIBRARY_PATH="${NIX_LIBS}${LD_LIBRARY_PATH}"
fi

if [ "$(is_musl)" = "true" ]; then
    # musl-based distributions
    if [ ! -d /tmp/local_files/alpine_glibc ]; then
        echo "Alpine Linux system is not supported yet"
        exit 1
    fi

    sed -i "s|https://.*alpinelinux.org|https://mirrors.aliyun.com|g" /etc/apk/repositories
    apk add bash
    apk add --allow-untrusted --force-overwrite /tmp/local_files/alpine_glibc/*.apk
    mkdir -p /lib64
    ln -sf /usr/glibc-compat/lib/ld-linux-x86-64.so.2 /lib64/ld-linux-x86-64.so.2
    ln -sf /usr/glibc-compat/lib/ld-linux-x86-64.so.2 /lib/ld-linux-x86-64.so.2
    mkdir -p "${LOG_DIR}"
    /tmp/miniforge/bin/rocklet --port ${port} >> "${LOG_DIR}/rocklet_uvicorn.log" 2>&1
else
    # glibc-based distributions
    mkdir -p "${LOG_DIR}"
    /tmp/miniforge/bin/rocklet --port ${port} >> "${LOG_DIR}/rocklet_uvicorn.log" 2>&1
fi