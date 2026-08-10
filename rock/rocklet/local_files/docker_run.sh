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

# Kata guest KVM is a misc character device. Its number belongs to the guest
# kernel and can differ from the outer host's /dev/kvm device number.
setup_kata_guest_kvm() {
    local kvm_major=10
    local kvm_minor=232
    local registered_minor

    if [ ! -r /proc/misc ]; then
        echo "Kata guest KVM setup failed: /proc/misc is unavailable." >&2
        return 1
    fi
    registered_minor=$(awk '$2 == "kvm" { print $1; exit }' /proc/misc)
    if [ "${registered_minor}" != "${kvm_minor}" ]; then
        echo "Kata guest KVM setup failed: expected misc device ${kvm_major}:${kvm_minor}, found minor ${registered_minor:-none}." >&2
        return 1
    fi

    rm -f /dev/kvm
    mknod -m 600 /dev/kvm c "${kvm_major}" "${kvm_minor}"
}

# Kata DinD: set up loop device and mount disk image for Docker storage
setup_kata_dind() {
    local docker_root="/var/lib/docker"
    if [ -f /etc/docker/daemon.json ]; then
        local custom_root
        custom_root=$(grep -o '"data-root"[[:space:]]*:[[:space:]]*"[^"]*"' /etc/docker/daemon.json | sed 's/.*"data-root"[[:space:]]*:[[:space:]]*"\([^"]*\)"/\1/')
        if [ -n "$custom_root" ]; then
            docker_root="$custom_root"
        fi
    fi
    # Directory creation requires write and execute permissions on the nearest existing parent.
    if [ ! -d "$docker_root" ]; then
        local nearest_existing_path="$docker_root"
        while [ ! -e "$nearest_existing_path" ]; do
            nearest_existing_path=$(dirname "$nearest_existing_path")
        done
        if [ ! -d "$nearest_existing_path" ] || [ ! -w "$nearest_existing_path" ] || [ ! -x "$nearest_existing_path" ]; then
            echo "Warning: no permission to create Docker data root '$docker_root'; skipping Kata DinD setup." >&2
        else
            mkdir -p "$docker_root"
        fi
    fi

    if [ -d "$docker_root" ]; then
        for i in $(seq 0 7); do
            mknod -m 660 /dev/loop$i b 7 $i 2>/dev/null || true
        done
        mount -o loop /docker-disk.img "$docker_root"
        mount -o remount,rw /sys/fs/cgroup
        mount -o remount,rw /proc/sys
    fi
}

# Run rocklet
if [ "$(is_nix)" = "true" ]; then
    # NixOS
    ln -sf $(ls -d /nix/store/*glibc*/lib 2>/dev/null | head -1) /lib
    ln -sf $(ls -d /nix/store/*glibc*/lib64 2>/dev/null | head -1) /lib64
    mkdir -p /bin
    ln -sf $(ls -d /nix/store/*bash*/bin/bash 2>/dev/null | head -1) /bin/bash
    ln -sf $(ls -d /nix/store/*util-linux*/bin/mount 2>/dev/null | head -1) /bin/mount
    export PATH="/bin:${PATH}"
    GCC_LIB=$(ls -d /nix/store/*gcc*lib/lib 2>/dev/null | head -1)
    ZLIB_LIB=$(ls -d /nix/store/*zlib*/lib 2>/dev/null | head -1)
    NIX_LIBS=""
    [ -n "$GCC_LIB" ] && NIX_LIBS="${GCC_LIB}:"
    [ -n "$ZLIB_LIB" ] && NIX_LIBS="${NIX_LIBS}${ZLIB_LIB}:"
    [ -n "$NIX_LIBS" ] && export LD_LIBRARY_PATH="${NIX_LIBS}${LD_LIBRARY_PATH}"
fi

if [ "${ROCK_KATA_RUNTIME}" = "true" ]; then
    echo "Kata runtime detected, setting up guest KVM and DinD disk..."
    if ! setup_kata_guest_kvm; then
        echo "WARNING: Kata guest KVM setup failed; continuing without /dev/kvm." >&2
    fi
    setup_kata_dind
fi

if [ "$(is_musl)" = "true" ]; then
    # musl-based distributions
    if [ ! -d /tmp/local_files/alpine_glibc ]; then
        echo "Alpine Linux system is not supported yet"
        exit 1
    fi

    sed -i "s|https://.*alpinelinux.org|https://mirrors.aliyun.com|g" /etc/apk/repositories
    command -v bash >/dev/null 2>&1 || apk add bash
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
