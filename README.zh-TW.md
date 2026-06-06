# Hermes Tool Filter

**讓 Hermes 工具調用在任何客戶端都能正確顯示的透明 SSE 代理**

<p align="center">
  <a href="README.md">English</a> · 繁體中文
</p>

<p align="center">
  <a href="https://github.com/fastapi/fastapi"><img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python&logoColor=white"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square"></a>
</p>

---

## 問題：Open WebUI 中的工具上下文丟失

當使用 **Hermes Agent 作為伺服器端工具執行器** 透過 Open WebUI 時，模型會產生**完全失憶**——工具調用（tool call）的結果在 tool loop 結束後被遺忘。模型無法參考之前的工具輸出，導致行為混亂與錯誤。

### 證據：Token 數量分析

從 `metrics_proxy` 捕獲的數據清楚顯示了問題：

```
時間         模型              輸入量   輸出量  TTFT      TG/s    總時長
05:46:45    qwen-27b-default  18698    396     1,578ms   37.8    12,062ms
05:46:27    qwen-27b-default  21240    384     2,258ms   41.2    11,568ms
05:46:15    qwen-27b-default  19576    72      2,045ms   55.1    3,353ms
05:46:12    qwen-27b-default  18301    57      1,195ms   61.0    2,130ms
```

**關鍵發現：**
- `05:46:12 → 05:46:27`：prompt_tokens 從 18,301 增加到 21,240（**+2,939 tokens**，tool loop 期間）
- `05:46:45`（tool loop 結束後的新訊息）：prompt_tokens 只有 18,698——只比原始多出 **+397 tokens**
- **如果工具結果被正確包含，05:46:45 應該有 ~21,240+ tokens**
- **實際只有 18,698——工具結果根本不在對話歷史中**

這證明 Open WebUI 在重構對話時**沒有包含工具結果**，導致模型失去所有工具執行的上下文。

### 根本原因：層層斷鏈

問題是三個層級的連鎖失敗：

```
Hermes Gateway API Server
  │
  │ 問題 1：從未發出標準 OpenAI tool_calls delta 格式
  │         只發送自訂 hermes.tool.progress SSE 事件
  │ 問題 2：completed 事件不包含工具結果
  │         (function_result 存在但從未放入 payload)
  │
  ▼
hermes_tool_filter
  │
  │ 問題 3：enhance-v2 模式未實作（實際上變成 passthrough）
  │ 問題 4：即使 enhance 模式也無法取得結果（因為問題 2）
  │         final_result = parsed_json.get("result", "") ← 永遠空白
  │
  ▼
Open WebUI
  │
  │ 問題 5：收到只有最終文字的 assistant message
  │         工具資訊從未進入 conversation history
  │ 問題 6：未配對的 tool_use/tool_result 訊息被丟棄
  │
  ▼
下一次請求
  │
  │ 問題 7：Open WebUI 回傳不含工具結果的 messages
  │         模型完全看不到之前的工具執行
  │
  ▼
模型失憶！🧠💥
```

### 詳細分析

**第一層 — Hermes Gateway API Server**（`gateway/platforms/api_server.py`）：

API Server 內部執行完整的 agent loop（正確維護包含工具結果的對話歷史）。但它的 SSE 輸出有兩個關鍵缺失：

1. **沒有標準 `tool_calls` delta**：不發送 `delta.tool_calls` 或 `role: "tool"` 訊息——只有自訂的 `hermes.tool.progress` 事件。
2. **completed 事件缺少 result**：`_on_tool_complete` callback 收到 `function_result` 但從未放入 SSE payload：
   ```python
   ("__tool_progress__", {
       "tool": function_name,
       "toolCallId": tool_call_id,
       "status": "completed",       # ← 沒有 "result" 欄位！
   })
   ```

**第二層 — hermes_tool_filter**（`main.py`）：

Filter 配置為 `enhance-v2`，但程式碼只檢查 `TOOL_MODE == "enhance"`，所以實際上一直是透傳模式。即使在 enhance 模式下，`result` 欄位也永遠是空的，因為 API Server 沒有發送它。

**第三層 — Open WebUI**：

Open WebUI 收到的是只有最終回覆文字的 assistant message。因為串流中沒有標準的 tool_calls/tool 訊息，它沒有任何東西可以存入對話資料庫。當用戶發送新訊息時，Open WebUI 重構的歷史中沒有任何工具上下文。

**驗證：Hermes Gateway 內部是正確的。** 從 `kv_cache.log`：
```
19:26:01 | prompt_tokens=32833 | msgs=14 | tools_def=26
  system: 1 messages, 23897 chars
  user: 1 messages, 1282 chars
  assistant: 6 messages, 356 chars
  tool: 6 messages, 37033 chars  ← 工具結果確實存在於內部
```

這確認問題出在 **SSE 轉換管線**，而非 Hermes 本身。

---

## 解決方案

Hermes Tool Filter 在 Hermes Gateway 與 Open WebUI 之間架起橋樑，將 Hermes 的自訂 SSE 格式轉換為 Open WebUI 可以解析、渲染並持久化的格式。

**enhance-v2 模式**（預設，推薦）：即時串流 + `<details>` 子標籤格式，完全相容 OpenWebUI：

```html
<!-- 經過 enhance-v2 轉換後： -->
<details type="tool_calls" done="true" name="web_search">
<summary>Done</summary>
<arguments>台北天氣 今天</arguments>
<result>{"success":true,"data":"..."}</result>
</details>
```

**enhance 模式**：過濾掉中間狀態的標籤，只在工具完成時注入一個符合標準的 `done="true"` 標籤。

**passthrough 模式**：直接透傳，適合已支援 Hermes 格式的客戶端。

**strip 模式**：移除 `<details>` 並替換為純文字 Markdown（舊版兼容）。

---

## 系統架構

```
使用者瀏覽器
    │
    ▼
Open WebUI (30010)
    │ POST http://127.0.0.1:9099/30001/v1/chat/completions
    ▼
hermes_tool_filter (9099)
    │ 模式: enhance-v2
    │ 轉換 hermes.tool.progress 事件 → <details> HTML 標籤
    │ 路由: /30001/v1/* → http://127.0.0.1:30001/v1/*
    ▼
Hermes Gateway API Server (30001)
    │ 基於 aiohttp, OpenAI-compatible endpoint
    │ 內部執行完整 agent loop 與工具執行
    ▼
vLLM (後端)
    │
    └── metrics_proxy (18080) 監控 vLLM 實例
```

> **注意**：metrics_proxy 不在 Hermes Gateway 的資料路徑中——它監控的是獨立的 vLLM 端口。

### 請求流程

1. **Open WebUI** 發送請求到 `hermes_tool_filter`（port 9099）
2. **hermes_tool_filter** 轉發到 **Hermes Gateway**（port 30000）
3. **Hermes Gateway** 執行 tool loop（內部維護完整對話歷史）
4. **Hermes Gateway** 回傳 SSE 串流給 **hermes_tool_filter**
5. **hermes_tool_filter** 轉換 SSE 格式後回傳給 **Open WebUI**
6. **Open WebUI** 接收回應並存儲到本地資料庫
7. 用戶發送新訊息 → **Open WebUI 重構對話歷史**並發送新請求

---

## 快速開始

```bash
git clone https://github.com/uraniumchonk/hermes-open-webui-adapter.git
cd hermes-open-webui-adapter
pip install -r requirements.txt
python main.py
```

服務啟動在 `http://0.0.0.0:9099`

將 Open WebUI 的 API Base URL 設為：

```
http://127.0.0.1:9099/30000/v1
```

---

## 配置

編輯 `config.yaml` 即可調整設定：

- **tool_mode** — `enhance-v2`（預設，推薦）/ `enhance` / `passthrough` / `strip`
- **auto_split_threshold** — 串流自動分割閾值（字元數，`0` = 關閉）
- **bind_host / bind_port** — 監聽位址與埠號

環境變數可覆蓋 config.yaml 設定（`TOOL_MODE`, `BIND_PORT`, `BIND_HOST`, `AUTO_SPLIT_THRESHOLD`）。

### 路由表

| 路徑 | 上游 | 用途 |
|------|------|------|
| `/30000/v1/*` | `127.0.0.1:30000` | Default |
| `/30001/v1/*` | `127.0.0.1:30001` | Coder |
| `/30002/v1/*` | `127.0.0.1:30002` | Analyst |
| `/30003/v1/*` | `127.0.0.1:30003` | Trader |

---

## 工作方式

1. 客戶端發送請求到代理（Port 9099）
2. 代理根據路徑轉發到對應的 Hermes Gateway
3. Gateway 回傳 SSE 串流（含 `hermes.tool.progress` 事件）
4. 代理即時解析並轉換為標準 `<details>` 格式
5. 回傳乾淨的串流給客戶端

---

## 功能

- **格式轉換** — 將 Hermes 自訂格式轉為客戶端可渲染的標準格式
- **四種處理模式** — enhance-v2（預設）、enhance、passthrough、strip
- **多租戶路由** — 一個代理對應多個 Gateway profiles
- **智能過濾** — enhance-v2 自動過濾 hermes.tool.progress 事件，只輸出完成標籤
- **完整工具資訊** — 注入的標籤包含工具名稱、參數、結果（子標籤格式）
- **config.yaml 配置** — 集中管理，無需修改程式碼

---

## Systemd 服務

```ini
[Unit]
Description=Hermes Tool Card Enhancer Proxy
After=network-online.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/hermes_tool_filter
ExecStart=/path/to/venv/bin/python main.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now hermes-tool-filter
```

---

## 🔧 Hermes API Server Patch 指南

> **為什麼需要 Patch？**
>
> Hermes Gateway 的 `_on_tool_complete` 函數沒有在 `__tool_progress__` completed 事件中發送 `arguments` 和 `result` 欄位，導致 hermes_tool_filter 無法取得工具參數與結果來建構 `<details>` 標籤。

### 自動套用（推薦）

```bash
# 檢查 patch 是否已套用
grep -c '"result": str(function_result)' /opt/hermes/hermes-agent/gateway/platforms/api_server.py

# 如果回傳 0，表示未套用，執行：
cd ~/.hermes/hermes/hermes-agent
git apply /path/to/hermes_tool_filter/patches/api_server_tool_result.patch 2>/dev/null || \
  echo "Patch 無法套用 — 可能已套用或 Hermes 版本已變更"
```

### 手動套用

1. **找到 `_on_tool_complete` 函數**（在 `gateway/platforms/api_server.py`，約第 1858 行）

2. **在 `progress_data` 字典中加入 `arguments` 和 `result`**：

```python
async def _on_tool_complete(self, tool_call_id, function_name, function_args, function_result):
    # ... 其他程式碼 ...
    progress_data = {
        "type": "__tool_progress__",
        "event": "completed",
        "tool_call_id": tool_call_id,
        "name": function_name,
        "arguments": function_args or {},          # ← 新增
        "result": str(function_result) if function_result is not None else "",  # ← 新增
    }
```

3. **重啟 Hermes Gateway**：

```bash
# 找到並停止舊程序
ps aux | grep "hermes.*gateway"
kill <PID>

# 啟動新的 Gateway
python -m hermes_cli.main --profile chatting gateway run --replace
```

### 維護：Hermes 更新後

`hermes update`（git pull/reset）會覆蓋手動修改！每次更新 Hermes 後：

```bash
# 方法 1：用 patch 檔案
cd ~/.hermes/hermes/hermes-agent
git apply /path/to/hermes_tool_filter/patches/api_server_tool_result.patch

# 方法 2：手動檢查
grep '"result":' /opt/hermes/hermes-agent/gateway/platforms/api_server.py
# 如果找不到，表示 patch 被覆蓋了，需要重新套用
```

### 驗證 Patch 是否生效

```bash
# 測試腳本
python3 /path/to/hermes_tool_filter/test_api_server.py

# 預期：completed 事件包含 arguments 和 result 欄位
```

---

## 技術細節

- **依賴** — FastAPI, aiohttp, PyYAML
- **架構** — 單檔案，無外部資料庫，零狀態
- **部署** — systemd 或直接執行

---

<p align="center">
  <sub>MIT License</sub>
</p>
