#!/bin/bash
#
# hermes-tool-filter 更新腳本 (v2 - 直接啟動模式)
# 用途：安全地停止舊服務、應用新代碼、重新啟動
#
# 用法: bash /home/user/hermes_tool_filter/restart.sh

set -e

SERVICE_NAME="hermes-tool-filter"
PORT=9099
PID_FILE="/home/user/hermes_tool_filter/.pid"
LOG_FILE="/home/user/hermes_tool_filter/server.log"
VENV_PYTHON="/opt/hermes/hermes-agent/venv/bin/python"
MAIN_PY="/home/user/hermes_tool_filter/main.py"
WORK_DIR="/home/user/hermes_tool_filter"

echo "═══════════════════════════════════════"
echo "  Hermes Tool Filter - 更新重啟腳本"
echo "═══════════════════════════════════════"
echo ""

# Step 1: 強制殺掉佔用 port 的程序
echo "🔍 檢查 port $PORT 是否被佔用..."
PID=$(fuser $PORT/tcp 2>/dev/null || true)

if [ -n "$PID" ]; then
    echo "⚠️  發現舊程序佔用 port $PORT (PID: $PID)"
    echo "🔪 強制終止舊程序..."
    
    # 嘗試 gracefully 停止
    kill $PID 2>/dev/null || true
    sleep 2
    
    # 如果還在，強制 kill
    REMAINING=$(fuser $PORT/tcp 2>/dev/null || true)
    if [ -n "$REMAINING" ]; then
        echo "⚠️  第一次失敗，嘗試 kill -9..."
        kill -9 $REMAINING 2>/dev/null || true
        sleep 1
    fi
    
    echo "✅ 舊程序已終止"
else
    echo "✅ Port $PORT 目前未被佔用"
fi

# Step 2: 清除 Python 快取
echo ""
echo "🧹 清除 Python __pycache__..."
find $WORK_DIR -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
echo "✅ 已清除 __pycache__"

# Step 3: 再次確認 port 已釋放
echo ""
echo "🔍 再次確認 port 狀態..."
FINAL_CHECK=$(fuser $PORT/tcp 2>/dev/null || true)
if [ -n "$FINAL_CHECK" ]; then
    echo "⚠️  Port 仍被佔用，最後一次 kill -9..."
    kill -9 $FINAL_CHECK 2>/dev/null || true
    sleep 2
fi

# Step 4: 嘗試用 systemd 啟動，失敗則用 nohup 直接啟動
echo ""
echo "🚀 嘗試啟動服務..."

# 嘗試 systemd (如果可用)
if systemctl start $SERVICE_NAME 2>/dev/null; then
    sleep 2
    STATUS=$(systemctl is-active $SERVICE_NAME 2>/dev/null || echo "unknown")
    if [ "$STATUS" = "active" ]; then
        echo "✅ systemd 啟動成功！"
        SYSTEMD_MODE=true
    else
        echo "⚠️  systemd 啟動失敗，改用直接啟動..."
        SYSTEMD_MODE=false
    fi
else
    echo "⚠️  systemd 不可用，改用直接啟動..."
    SYSTEMD_MODE=false
fi

# 如果 systemd 失敗，用 nohup 直接啟動
if [ "$SYSTEMD_MODE" != "true" ]; then
    echo "🚀 直接啟動 Python 程序..."
    
    cd $WORK_DIR
    nohup $VENV_PYTHON $MAIN_PY > $LOG_FILE 2>&1 &
    NEW_PID=$!
    echo $NEW_PID > $PID_FILE
    
    echo "✅ 程序已啟動 (PID: $NEW_PID)"
    echo "   PID 檔案: $PID_FILE"
    echo "   日誌檔案: $LOG_FILE"
fi

# Step 5: 等待並驗證
echo ""
echo "⏳ 等待服務啟動..."
sleep 3

# 健康檢查
echo "🏥 執行健康檢查..."
HEALTH=$(curl -s --connect-timeout 3 http://127.0.0.1:$PORT/health 2>/dev/null || echo "failed")

echo ""
echo "═══════════════════════════════════════"

if echo "$HEALTH" | grep -q '"status"'; then
    echo "✅ 健康檢查通過！"
    echo ""
    echo "📊 路由配置:"
    echo "$HEALTH" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for port, url in data.get('ports', {}).items():
    print(f'   /{port}/v1/*  ->  {url}/v1/*')
print(f'   預設: {data.get(\"default_upstream\", \"\")}')
" 2>/dev/null || echo "   (解析失敗)"
    
    echo ""
    echo "✅ 更新完成！"
    echo ""
    echo "📝 管理指令:"
    echo "   查看日誌: tail -f $LOG_FILE"
    echo "   停止服務: kill \$(cat $PID_FILE)"
    if [ "$SYSTEMD_MODE" = "true" ]; then
        echo "   systemd: systemctl stop $SERVICE_NAME"
    fi
else
    echo "❌ 啟動失敗！"
    echo ""
    echo "📋 最後 10 行日誌:"
    tail -10 $LOG_FILE 2>/dev/null || echo "   (無日誌)"
    echo ""
    echo "🔧 手動啟動:"
    echo "   cd $WORK_DIR"
    echo "   $VENV_PYTHON $MAIN_PY"
    exit 1
fi

echo ""
echo "═══════════════════════════════════════"
