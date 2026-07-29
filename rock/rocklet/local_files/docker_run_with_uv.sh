#!/bin/bash
set -o errexit

PROJECT_ROOT=$1
port=$2

if [ -z "$PROJECT_ROOT" ]; then
    echo "Error: PROJECT_ROOT is required as first argument"
    exit 1
fi

if [ ! -f /etc/alpine-release ]; then
    # Not Alpine Linux system
    # Run rocklet

    # Check if uv is already installed
    if command -v uv &> /dev/null; then
        echo "uv is already installed, skipping installation..."
        UV_CMD=uv
    # Install uv - check if pip is available
    elif command -v pip &> /dev/null || command -v pip3 &> /dev/null; then
        echo "Installing uv via pip..."
        if command -v pip3 &> /dev/null; then
            pip3 install uv -i https://mirrors.aliyun.com/pypi/simple/
        else
            pip install uv -i https://mirrors.aliyun.com/pypi/simple/
        fi
        UV_CMD=uv
    else
        echo "pip not found, installing uv via installation script..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        UV_CMD=$HOME/.local/bin/uv
    fi

    # Copy project to a writable directory (source mount is read-only)
    # Use tar to exclude large unnecessary directories for faster copy
    WRITABLE_PROJECT=/tmp/rock-build
    mkdir -p $WRITABLE_PROJECT
    tar -cf - --exclude='.venv' --exclude='.git' --exclude='__pycache__' \
        --exclude='*.egg-info' --exclude='.pytest_cache' --exclude='.ruff_cache' \
        -C $PROJECT_ROOT . | tar -xf - -C $WRITABLE_PROJECT
    cd $WRITABLE_PROJECT

    # Create virtual environment
    $UV_CMD venv --python 3.11 /tmp/rocklet-venv

    # Install dependencies
    $UV_CMD pip install --python /tmp/rocklet-venv/bin/python ".[rocklet]"

    # Clean up build directory to free disk space
    rm -rf $WRITABLE_PROJECT


    mkdir -p /data/logs
    # Run rocklet
    /tmp/rocklet-venv/bin/rocklet --port ${port} >> /data/logs/rocklet.log 2>&1

else
    echo "Alpine Linux system is not supported yet"
    exit 1
fi
