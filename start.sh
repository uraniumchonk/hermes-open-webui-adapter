#!/bin/bash
# Hermes Tool Filter 啟動腳本
# 使用方式: ./start.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 使用 Hermes 的 venv
VENV_PYTHON="/opt/hermes/hermes-agent/venv/bin/python3"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "❌ 找不到 Hermes venv python: $VENV_PYTHON"
    exit 1
fi

# 停止舊的 filter 進程
echo "🔍 停止舊的 filter 進程..."
pkill -f "python.*hermes_tool_filter" 2>/dev/null || true
sleep 1

# 確認端口 9099 空閒
if lsof -i :9099 >/dev/null 2>&1; then
    echo "⚠️  端口 9099 仍被佔用，強制關閉..."
    fuser -k 9099/tcp 2>/dev/null || true
    sleep 1
fi

# 啟動 filter
echo "🚀 啟動 Hermes Tool Filter (port 9099)..."
nohup "$VENV_PYTHON" main.py > /tmp/filter.log 2>&1 &
FILTER_PID=$!

echo "✅ Filter 已啟動，PID: $FILTER_PID"
echo "📋 日誌: /tmp/filter.log"
echo "🌐 端點: http://127.0.0.1:9099"

# 等待 2 秒確認是否正常運行
sleep 2
if kill -0 $FILTER_PID 2>/dev/null; then
    echo "✅ 運行中！"
else
    echo "❌ 啟動失敗，查看日誌:"
    tail -20 /tmp/filter.log
    exit 1
fi
