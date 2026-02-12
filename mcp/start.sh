#!/bin/bash

# ROCK MCP Tools 启动脚本

echo "🚀 Starting ROCK MCP Tools..."

# 检查Python环境
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found!"
    exit 1
fi

# 检查依赖
echo "📦 Checking dependencies..."
python3 -c "import mcp" 2>/dev/null || {
    echo "❌ MCP not installed. Installing..."
    pip install mcp
}

python3 -c "import asyncio" 2>/dev/null || {
    echo "❌ asyncio not available"
    exit 1
}

# 设置环境变量
export ROCK_ADMIN_URL=${ROCK_ADMIN_URL:-"http://127.0.0.1:8080"}
export ROCK_API_KEY=${ROCK_API_KEY:-""}
export ROCK_TIMEOUT=${ROCK_TIMEOUT:-"30"}

echo "🔧 Configuration:"
echo "   - ROCK_ADMIN_URL: $ROCK_ADMIN_URL"
echo "   - ROCK_TIMEOUT: $ROCK_TIMEOUT seconds"

# 启动MCP服务器
echo "▶️  Starting MCP server..."
python3 rock_mcp_server.py