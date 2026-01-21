#!/bin/bash
set -o errexit

port=$1

if [ ! -f /etc/alpine-release ]; then
    # Not Alpine Linux system
    # Run rocklet
    mkdir -p /data/logs
    /tmp/miniforge/bin/rocklet --port ${port} >> /data/logs/rocklet_uvicorn.log 2>&1

else
    echo "Alpine Linux system is not supported yet"
fi
