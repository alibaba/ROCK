#!/bin/bash
set -o errexit

if [ ! -f /etc/alpine-release ]; then
    if command -v pip3 &> /dev/null; then
        PIP_CMD=pip3
    elif command -v pip &> /dev/null; then
        PIP_CMD=pip
    else
        if command -v apt-get &> /dev/null; then
            apt-get update
            apt-get install -y python3 python3-pip
            PIP_CMD=pip3
        elif command -v yum &> /dev/null; then
            yum install -y python3 python3-pip
            PIP_CMD=pip3
        fi
    fi

    $PIP_CMD install rl-rock[rocklet] -i https://mirrors.aliyun.com/pypi/simple/

    mkdir -p /data/logs
    rocklet >> /data/logs/rocklet.log 2>&1
else
    exit 1
fi
