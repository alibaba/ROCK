#!/bin/bash
set -o errexit

if [ ! -f /etc/alpine-release ]; then
    # Not Alpine Linux system
    # Run rocklet
    mkdir -p /data/logs
    /tmp/miniforge/bin/rocklet >> /data/logs/rocklet.log 2>&1

else
    echo "Alpine Linux system is not supported yet"
fi
