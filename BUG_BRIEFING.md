# hermes_tool_filter Bug 手冊 — 給下一個小喵

## 問題事實

1. **原始目標**：讓 tools 在 Conduit APP 串流時顯示為「已完成」，不再卡在「正在執行」
2. **已完成的修改**：
   - 加入 `<details done="true">` 注入邏輯，讓 Open WebUI 工具卡片正確更新狀態
   - 加入 User-Agent 偵測區分 Conduit (strip `<details>`) vs Open WebUI (保留)
3. **當前 bug**：一個訊息跑完後，再傳第二個訊息時，串流開始會噴錯誤
4. **用戶反饋**：「Conduit APP 和 Open WebUI 實際上應該是共用的，只是渲染不同 — APP 也只是連線到了 Open WebUI 的後端，Web 也是連接到」

## 關鍵檔案

- **主程式**：`/home/user/hermes_tool_filter/main.py` (540 行)
- **Open WebUI 設定**：`/opt/hermes/hermes-agent/venv/lib/python3.11/site-packages/open_webui/data/webui.db`
  - Open WebUI 的 API base URLs：`http://127.0.0.1:9099/30001/v1` (coder) 等
- **Open WebUI 啟動腳本**：`/home/user/start_openwebui.sh` (port 30010)

## 架構真相

```
Conduit APP  ──→ Open WebUI (port 30010) ──→ hermes_tool_filter (port 9099) ──→ Hermes Gateway (port 30000-30003)
Web 瀏覽器  ──→ Open WebUI (port 30010) ──→ hermes_tool_filter (port 9099) ──→ Hermes Gateway
```

**重要**：Conduit APP 不是直接連 hermes_tool_filter，而是透過 Open WebUI 後端！所以 User-Agent 區分可能不正確，因為 Open WebUI 作為中間層會用自己的 UA。

## 存取方式

```bash
# 查看 hermes_tool_filter 程式碼
cat /home/user/hermes_tool_filter/main.py

# 重啟服務
pkill -f 'python.*hermes_tool_filter/main.py'
cd /home/user/hermes_tool_filter
/opt/hermes/hermes-agent/venv/bin/python main.py &

# 測試健康檢查
curl http://127.0.0.1:9099/health

# 查看 Open WebUI 設定的 API URL
python3 -c "
import sqlite3, json
conn = sqlite3.connect('/opt/hermes/hermes-agent/venv/lib/python3.11/site-packages/open_webui/data/webui.db')
cursor = conn.cursor()
cursor.execute('SELECT data FROM config LIMIT 5')
for r in cursor.fetchall():
    d = json.loads(r[0]) if r[0] else {}
    for k,v in d.items():
        if 'openai' in k.lower() or '9099' in str(v).lower():
            print(f'{k}: {json.dumps(v, indent=2)[:500]}')
conn.close()
"
```

## 測試方法

1. 在 Conduit APP 發送訊息 → 觀察工具卡片狀態
2. 等訊息完成後，再發第二條訊息 → 觀察是否噴錯
3. 在 Web 版 Open WebUI 做同樣測試
4. 檢查 hermes_tool_filter 的日誌輸出

## 當前程式碼重點

- `transform_stream()`: SSE 串流轉換核心，`strip_details` 參數控制是否移除 `<details>` 標籤
- `proxy_with_transform()`: 帶 port prefix 的路由 (line 328)
- `proxy_default()`: 無 port prefix 的備用路由 (line 429)
- 兩個路由都使用相同的 UA 偵測邏輯決定 `strip_details`

## 待解決

- 第二條訊息串流起始錯誤的根本原因
- 是否需要重新設計客戶端區分方式（因為都經過 Open WebUI）
