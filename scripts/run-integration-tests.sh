#!/bin/bash
# Run full integration tests inside a Docker container (Linux environment).
# This solves the issue where Ray cannot run stably on macOS (Apple Silicon).
#
# Usage:
#   ./scripts/run-integration-tests.sh              # Run all integration tests
#   ./scripts/run-integration-tests.sh tests/unit    # Run unit tests only
#
# Prerequisites:
#   - Docker must be running (OrbStack / Docker Desktop)
#   - python:3.11 image will be used as the base

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_PATH="${1:-tests/integration}"
CONTAINER_NAME="rock-test-runner-$$"
BASE_IMAGE="rock-test-base:latest"

echo "============================================"
echo "  ROCK Integration Test Runner (Docker)"
echo "============================================"
echo ""

# Check Docker is available
if ! docker version > /dev/null 2>&1; then
    echo "❌ Docker is not running. Please start Docker (OrbStack) first."
    exit 1
fi
echo "✅ Docker is running"

# Build base image with Docker CLI + uv pre-installed (cached after first build)
if ! docker images "${BASE_IMAGE}" --format '{{.Repository}}' | grep -q rock-test-base; then
    echo "📦 Building base test image (first time only, will be cached)..."
    docker build -t "${BASE_IMAGE}" -f - . <<'DOCKERFILE'
FROM docker:cli AS docker-cli
FROM python:3.11
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
RUN pip install --no-cache-dir uv
DOCKERFILE
    echo "✅ Base image built and cached"
else
    echo "✅ Base image found (cached)"
fi
echo ""

echo "🚀 Starting test container..."
echo "   Test path: ${TEST_PATH}"
echo ""

# Run tests inside a privileged container with Docker socket access.
# --privileged is needed for Docker-in-Docker (sandbox containers).
# Ray resources are set high enough to satisfy test requirements (CPU: 2, memory: 8GB).
docker run --rm \
    --name "${CONTAINER_NAME}" \
    --privileged \
    -v "${PROJECT_ROOT}:/workspace" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -w /workspace \
    -e DOCKER_HOST=unix:///var/run/docker.sock \
    -e UV_HTTP_TIMEOUT=120 \
    --memory=16g \
    --cpus=8 \
    "${BASE_IMAGE}" bash -c "
        set -e

        echo '📦 Installing project dependencies...'
        uv sync --all-extras --group test --quiet
        echo '✅ Dependencies installed'

        echo '⚡ Starting Ray cluster...'
        uv run ray start --head --disable-usage-stats --num-cpus=8 --memory=17179869184
        sleep 3

        # Verify Ray is running
        if uv run ray status > /dev/null 2>&1; then
            echo '✅ Ray cluster is running'
        else
            echo '⚠️  Ray cluster may not be fully ready, proceeding anyway...'
        fi

        echo ''
        echo '🧪 Running tests: ${TEST_PATH}'
        echo '============================================'
        mkdir -p .tmp/test_data/logs
        uv run pytest ${TEST_PATH} -v --timeout=300 --reruns 1

        echo ''
        echo '🧹 Stopping Ray...'
        uv run ray stop --force || true
        echo '✅ Done!'
    "
