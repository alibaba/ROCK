#!/bin/bash
# =============================================================================
# FC Hybrid Adapter Packaging Script
# =============================================================================
#
# 方案 C：打包 rocklet 代码和适配层用于 Python3.10 运行时部署
#
# 使用方式：
#   cd rock/deployments/fc_rocklet/adapter
#   ./package.sh
#
# 输出：dist/fc_rocklet_adapter.zip
#
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROCK_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/dist"
OUTPUT_FILE="$OUTPUT_DIR/fc_rocklet_adapter.zip"

echo "=== FC Hybrid Adapter Packaging ==="
echo "ROCK_ROOT: $ROCK_ROOT"
echo "OUTPUT: $OUTPUT_FILE"
echo ""

# 创建输出目录
mkdir -p "$OUTPUT_DIR"

# 创建临时目录
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

echo "[1/3] Copying rock package..."
cp -r "$ROCK_ROOT/rock" "$TEMP_DIR/rock"

echo "[2/3] Copying adapter server..."
cp "$SCRIPT_DIR/server.py" "$TEMP_DIR/server.py"

echo "[3/3] Creating zip archive..."
cd "$TEMP_DIR"
zip -rq "$OUTPUT_FILE" .

echo ""
echo "=== Packaging Complete ==="
echo "Output: $OUTPUT_FILE"
echo "Size: $(du -h "$OUTPUT_FILE" | cut -f1)"
echo ""
echo "Next steps:"
echo "  1. cd $SCRIPT_DIR"
echo "  2. Modify s.yaml if needed"
echo "  3. s deploy"
