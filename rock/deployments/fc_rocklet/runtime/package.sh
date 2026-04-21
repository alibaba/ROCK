#!/bin/bash
# =============================================================================
# FC Custom Runtime Packaging Script
# =============================================================================
#
# 方案 B：打包 rocklet 代码用于自定义运行时部署
#
# 使用方式：
#   cd rock/deployments/fc_rocklet/runtime
#   ./package.sh
#
# 输出：dist/fc_rocklet_runtime.zip
#
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROCK_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/dist"
OUTPUT_FILE="$OUTPUT_DIR/fc_rocklet_runtime.zip"

echo "=== FC Custom Runtime Packaging ==="
echo "ROCK_ROOT: $ROCK_ROOT"
echo "OUTPUT: $OUTPUT_FILE"
echo ""

# 创建输出目录
mkdir -p "$OUTPUT_DIR"

# 创建临时目录（在项目内但排除 dist 目录）
TEMP_DIR="$SCRIPT_DIR/.build_tmp"
rm -rf "$TEMP_DIR"
mkdir -p "$TEMP_DIR"
trap "rm -rf $TEMP_DIR" EXIT

echo "[1/5] Copying rock package (excluding deployments)..."
mkdir -p "$TEMP_DIR/rock"
for item in "$ROCK_ROOT/rock"/*; do
    name=$(basename "$item")
    if [ "$name" != "deployments" ]; then
        cp -r "$item" "$TEMP_DIR/rock/"
    fi
done

echo "[2/5] Copying bootstrap script..."
cp "$SCRIPT_DIR/bootstrap" "$TEMP_DIR/bootstrap"
chmod +x "$TEMP_DIR/bootstrap"

echo "[3/5] Copying requirements..."
cp "$SCRIPT_DIR/requirements.txt" "$TEMP_DIR/requirements.txt"

echo "[4/5] Installing dependencies for Linux x86_64..."
mkdir -p "$TEMP_DIR/deps"
python3 -m pip install -q -r "$SCRIPT_DIR/requirements.txt" -t "$TEMP_DIR/deps" \
    --platform manylinux2014_x86_64 \
    --implementation cp \
    --python-version 3.11 \
    --only-binary=:all:

echo "[5/5] Creating zip archive..."
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
