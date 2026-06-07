# Hermes Tool Filter v2.0.0

**讓 Hermes 工具調用在任何客戶端都能正確顯示的透明 SSE 代理**

<p align="center">
  <a href="README.md">English</a> · 繁體中文
</p>

<p align="center">
  <a href="https://github.com/fastapi/fastapi"><img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python&logoColor=white"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square"></a>
  <a href="#"><img src="https://img.shields.io/badge/Version-2.0.0-brightgreen?style=flat-square"></a>
</p>

> **解決問題**：Hermes Gateway 在 Open WebUI / Conduit APP 中工具卡片無法顯示、工具結果從對話上下文中丟失——導致模型失憶。

---

## 🌟 重點功能

- ⚡ **enhance-v2 模式** — 即時串流 + 完整 `<details>` 子標籤格式，完全相容 OpenWebUI
- 🏢 **多 Profile 路由** — 一個代理對應多個 Gateway profiles（Chatting / Coder / Analyst / Trader）
- 🧹 **歷史清理（防污染）** — 將對話歷史中的 `<details>` 標籤替換為自然語言（19 種模板），防止模型模仿輸出 HTML
- 📡 **雙 Handler 架構** — 分別處理 `/v1/chat/completions` 和 `/v1/responses`，Responses API 自動注入工具結果
- 🔧 **一鍵 Patch** — 包含自動套用 patch + 驗證腳本，修改 Hermes API Server
- 📦 **零狀態** — 無資料庫、config.yaml 驅動

---

## 目錄

- [問題：工具上下文丟失](#問題：工具上下文丟失)
- [解決方案](#解決方案)
- [系統架構](#系統架構)
- [快速開始](#快速開始)
- [配置](#配置)
- [工作方式](#工作方式)
- [功能](#功能)
- [歷史清理（防污染）](#歷史清理防污染)
- [Responses API 支援](#responses-api-支援)
- [Systemd 服務](#systemd-服務)
- [🔧 Hermes API Server Patch 指南](#-hermes-api-server-patch-指南)
- [故障排除](#故障排除)
- [技術細節](#技術細節)

---

## 問題：工具上下文丟失

當使用 **Hermes Agent 作為伺服器端工具執行器** 透過 Open WebUI 時，模型會產生**完全失憶**——工具調用（tool call）的結果在 tool loop 結束後被遺忘。模型無法參考之前的工具輸出，導致行為混亂與錯誤。

### 證據：Token 數量差異

當 tool loop 運行時，prompt_tokens 會大幅增加（工具結果在內部被包含）。但當用戶在 tool loop 結束後發送新訊息時，prompt_tokens 掉回接近原始數量——證明工具結果**沒有**被保存在對話歷史中。

**範例：**

| 步驟 | 說明 | prompt_tokens |
|------|------|---------------|
| 1 | 初始訊息 | ~18,301 |
| 2 | Tool loop（內部） | ~21,240 (+2,939) |
| 3 | 新訊息（tool loop 後） | ~18,698 |

- **步驟 3 預期**：~21,240+ tokens（工具結果在歷史中）
- **步驟 3 實際**：~18,698——工具結果**根本不在**對話歷史中

這證明 Open WebUI 重構對話時**沒有包含工具結果**，模型失去所有工具執行的上下文。

### 根本原因：層層斷鏈

```
Hermes Gateway API Server
  │
  │ ✗ 問題 1：從未發出標準 OpenAI tool_calls delta
  │         只發送自訂 hermes.tool.progress SSE 事件
  │ ✗ 問題 2：completed 事件不包含工具結果
  │         (function_result 存在但從未放入 payload)
  │
  ▼
hermes_tool_filter
  │
  │ ✗ 問題 3：enhance-v2 模式未實作（實際上變成 passthrough）
  │ ✗ 問題 4：即使 enhance 模式也無法取得結果（因為問題 2）
  │         final_result = parsed_json.get("result", "") ← 永遠空白
  │
  ▼
Open WebUI
  │
  │ ✗ 問題 5：收到只有最終文字的 assistant message
  │         工具資訊從未進入 conversation history
  │ ✗ 問題 6：未配對的 tool_use/tool_result 訊息被丟棄
  │
  ▼
下一次請求
  │
  │ ✗ 問題 7：Open WebUI 回傳不含工具結果的 messages
  │         模型完全看不到之前的工具執行
  │
  ▼
🧠💥 模型失憶！
```

### 詳細分析

**第一層 — Hermes Gateway API Server**（`gateway/platforms/api_server.py`）：

API Server 內部執行完整的 agent loop（正確維護包含工具結果的對話歷史）。但 SSE 輸出有兩個關鍵缺失：

1. **沒有標準 `tool_calls` delta**：不發送 `delta.tool_calls` 或 `role: "tool"` 訊息——只有自訂 `hermes.tool.progress` 事件。
2. **completed 事件缺少 result**：`_on_tool_complete` callback 收到 `function_result` 但從未放入：
   ```python
   ("__tool_progress__", {
       "tool": function_name,
       "toolCallId": tool_call_id,
       "status": "completed",       # ← 沒有 "result" 欄位！
   })
   ```

**第二層 — hermes_tool_filter**（`main.py`）：

配置為 `enhance-v2` 但程式碼只檢查 `TOOL_MODE == "enhance"`，所以實際上一直是透傳。即使在 enhance 模式下，`result` 也永遠空白。

**第三層 — Open WebUI**：

收到只有最終回覆文字的 assistant message。串流中沒有標準 tool 訊息，沒有任何東西可存入。用戶發送新訊息時，重構的歷史沒有工具上下文。

**✅ 驗證：Hermes Gateway 內部是正確的。** Hermes 內部的 KV cache 確認工具訊息與結果在 agent loop 期間被正確維護——問題純粹出在 SSE 輸出管線上。

問題出在 **SSE 轉換管線**，而非 Hermes 本身。

---

## 解決方案

Hermes Tool Filter 在 Hermes Gateway 與 Open WebUI 之間架起橋樑，將自訂 SSE 格式轉換為 Open WebUI 可以解析、渲染並持久化的格式。

**enhance-v2 模式**（預設，推薦）：

```html
<!-- 經過 enhance-v2 轉換後： -->
<details type="tool_calls" done="true" name="terminal">
<summary>✅ 💻 echo hello</summary>
<arguments>{"tool_name": "terminal", "command": "echo hello"}</arguments>
<result>{"output": "hello", "exit_code": 0, "error": null}</result>
</details>
```

- `<summary>` — 工具名稱 + emoji（供視覺識別）
- `<arguments>` — 完整 JSON，包含 `tool_name` + 參數（讓模型能識別工具）
- `<result>` — 工具執行結果

**enhance 模式**：過濾中間狀態，完成時注入 `done="true"` 標籤。

**passthrough 模式**：直接透傳，適合已支援 Hermes 格式的客戶端。

**strip 模式**：移除 `<details>`，替換為純 Markdown（舊版兼容）。

---

## 系統架構

```
使用者瀏覽器
    │
    ▼
Open WebUI (30010)
    │ POST http://127.0.0.1:9099/30001/v1/chat/completions
    │ POST http://127.0.0.1:9099/30001/v1/responses
    ▼
hermes_tool_filter (9099)
    │ completions_handler.py  ← /v1/chat/completions（enhance-v2 + 歷史清理）
    │ responses_handler.py    ← /v1/responses（工具注入 + SSE 透傳）
    │ 路由: /30001/v1/* → http://127.0.0.1:30001/v1/*
    ▼
Hermes Gateway API Server (30001)
    │ 基於 aiohttp, OpenAI-compatible endpoint
    │ 內部執行完整 agent loop 與工具執行
    ▼
vLLM（後端）
```

### 請求流程

1. **Open WebUI** → `hermes_tool_filter`（port 9099）
2. **代理** → **Hermes Gateway**（port 30000+）
3. **Gateway** 執行 tool loop（內部維護完整歷史）
4. **Gateway** → SSE 串流 → **代理**
5. **代理** 轉換格式 → **Open WebUI**
6. **Open WebUI** 存入本地資料庫
7. 用戶發送新訊息 → 歷史重構時包含工具上下文

### 雙 Handler 設計

代理使用兩個獨立的 handler 處理不同 API 格式：

| Handler | 路徑 | 功能 |
|---------|------|------|
| `completions_handler.py` | `/v1/chat/completions` | enhance-v2 SSE 轉換、歷史清理 |
| `responses_handler.py` | `/v1/responses` | 工具結果注入、SSE 透傳/轉換 |

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

編輯 `config.yaml`：

- **tool_mode** — `enhance-v2`（預設，推薦）/ `enhance` / `passthrough` / `strip`
- **auto_split_threshold** — 串流自動分割閾值（字元數，`0` = 關閉）
- **bind_host / bind_port** — 監聽位址與埠號
- **upstreams** — 靈活路由表（見下方）

環境變數可覆蓋 config.yaml（`TOOL_MODE`, `BIND_PORT`, `BIND_HOST`, `AUTO_SPLIT_THRESHOLD`）。

### 上游路由（可配置）

路由完全由 `config.yaml` 控制 — 增減 profile 不需要修改程式碼：

```yaml
upstreams:
  "30000": "http://127.0.0.1:30000"
  "30001": "http://127.0.0.1:30001"
  "30002": "http://127.0.0.1:30002"
  "30003": "http://127.0.0.1:30003"
```

每個鍵是路徑前綴，每個值是对應的 Hermes Gateway URL。常見範例：

- `30000` → 通用聊天
- `30001` → 程式開發專家
- `30002` → 資料與研究
- `30003` → 交易與市場

查看你的 profiles：`hermes profiles list`。只需在 `config.yaml` 中新增或移除項目即可。若省略 `upstreams`，會自動使用上方四個預設值。

### Hermes Gateway 配置

代理路由到多個 Hermes Gateway 實例。每個 Gateway profile 有獨立的 `.env` 檔案，位置取決於安裝方式：

```
/opt/hermes/profiles/<PROFILE_NAME>/.env     # 系統級安裝
~/.hermes/profiles/<PROFILE_NAME>/.env       # 使用者級安裝
```

例如，名為 `chatting` 的 profile 會在 `/opt/hermes/profiles/chatting/.env` 或 `~/.hermes/profiles/chatting/.env`。重要設定：

```bash
# 啟用 API 伺服器
API_SERVER_ENABLED=true

# Gateway 埠號 — 必須與 upstreams 中的埠號一致
API_SERVER_PORT=30000

# API 金鑰 — 用於驗證請求
API_SERVER_KEY=sk_你的自訂金鑰
```

> **重要：** `API_SERVER_PORT` 的值必須對應 `upstreams` 中的埠號。例如，若 `API_SERVER_PORT=30001`，則 upstreams 中必須包含 `"30001": "http://127.0.0.1:30001"`。

在 Open WebUI 中連接時，API Base URL 設為 `http://127.0.0.1:9099/<PORT>/v1`，API 金鑰設為 Gateway 的 `API_SERVER_KEY`。

---

## 工作方式

1. 客戶端發送請求到代理（Port 9099）
2. 代理根據路徑轉發到對應的 Hermes Gateway
3. Gateway 回傳 SSE 串流（含 `hermes.tool.progress` 事件）
4. 代理即時解析並轉換為標準 `<details>` 格式
5. 回傳乾淨的串流給客戶端

---

## 功能

- 🔄 **格式轉換** — Hermes 自訂格式 → 客戶端可渲染的標準格式
- 🎛️ **四種處理模式** — enhance-v2（預設）、enhance、passthrough、strip
- 🏢 **多租戶路由** — 一個代理，多個 Gateway profiles
- 🧹 **歷史清理** — 19 種自然語言模板，涵蓋 5 種工具類型，確定性隨機確保 vLLM KV Cache 命中率
- 📡 **雙 Handler** — 分別處理 Chat Completions 和 Responses API
- 💉 **工具結果注入** — Responses API 狀態模式下自動注入（雙路徑：原生 + 代理）
- 📋 **完整工具資訊** — 工具名稱、參數、結果（子標籤格式）
- ⚙️ **配置驅動** — 集中管理，無需修改程式碼

---

## 歷史清理（防污染）

**問題：** enhance-v2 注入的 `<details>` 標籤以純文字形式儲存在 Open WebUI 的對話歷史中。下一次請求時，模型在 prompt 中看到這些 HTML 標籤，開始模仿輸出——在回覆中加入 `<details>`、`<summary>`、`<arguments>`、`<result>`。這形成**污染反饋迴圈**，越聊越嚴重。

**解決方案：** 在將請求轉發給上游 Gateway 之前，代理掃描所有 assistant 訊息，將 `<details>` 區塊替換為自然語言描述。

### 工作原理

1. 攔截傳入的 `messages` 陣列
2. 針對每個 `role: "assistant"` 的訊息，尋找 `<details type="tool_calls">` 區塊
3. 從區塊中提取工具名稱、參數和結果
4. 用自然語言句子替換該區塊
5. 將清理後的請求轉發給 Gateway

### 範例

**清理前（被污染）：**
```
好的喵～讓我查一下喵～

<details type="tool_calls" done="true" name="web_search">
<summary>✅ 🌐 web_search</summary>
<arguments>{"tool_name": "web_search", "query": "BTC price today"}</arguments>
<result>{"data": [{"title": "Bitcoin Price", ...}]}</result>
</details>

BTC 現在的價格大約是...
```

**清理後（乾淨）：**
```
好的喵～讓我查一下喵～

搜尋「BTC price today」後獲得的資訊：{"data": [{"title": "Bitcoin Price", ...}]}

BTC 現在的價格大約是...
```

### 19 種自然語言模板

為了防止模型學會固定格式，清理功能使用 **19 種不同的句子模板**，涵蓋 5 種工具類型。確定性隨機選擇器（`random.Random(seed + index)`）確保相同輸入永遠產生相同輸出——這對 **vLLM KV Cache 命中** 至關重要。

| 工具類型 | 模板數 | 工具 |
|---------|--------|------|
| 搜尋 | 4 | `web_search`, `brave_web_search`, `search_files`, `session_search` |
| 交易 | 4 | `mcp_trading_get_positions`, `mcp_trading_get_wallet_balance`, ... |
| 檔案 | 3 | `read_file`, `write_file`, `patch` |
| 程式碼 | 3 | `execute_code`, `terminal` |
| 通用 | 5 | 其他所有工具 |

### 配置

透過 `config.yaml` 控制：

```yaml
enable_history_sanitization: true
sanitization_result_max_length: 2000
```

---

## Responses API 支援

**問題：** 當 Open WebUI 使用 Responses API 的有狀態模式（`previous_response_id`）時，`input` 中只帶入使用者訊息——上一輪的工具結果丟失。模型完全看不到之前執行了什麼工具、返回了什麼結果。

**解決方案：** `responses_handler.py` 取得上一輪的 response，將工具結果以文字摘要形式注入到當前 input 中。

### 雙路徑保護

- **路徑 A（原生）：** 保留 `previous_response_id`，讓 Hermes 內部取得完整的 `conversation_history`（主要機制）。
- **路徑 B（代理注入）：** 工具結果轉為文字摘要，前置到使用者訊息前作為備援。

### 注入格式

```xml
<tool_results_from_previous_turn>
[Previous turn] Tool called: web_search(query=BTC price today)
[Previous turn] Tool result: {"data": [{"title": "Bitcoin Price", ...}]}
</tool_results_from_previous_turn>

<使用者的新訊息>
```

### SSE 模式

| 模式 | 行為 |
|------|------|
| `passthrough`（預設） | 直接轉發 Responses SSE 事件 |
| `convert` | 將 Responses SSE 轉換為 Chat Completions SSE |

### 配置

```yaml
responses_sse_mode: "passthrough"  # 或 "convert"
```

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

`hermes update`（git pull/reset）會覆蓋手動修改！每次更新後：

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

## 故障排除

| 症狀 | 原因 | 解決方案 |
|------|------|---------|
| Open WebUI 中工具卡片不顯示 | enhance-v2 未啟用 | 確認 config.yaml 中 `tool_mode: "enhance-v2"` |
| `<details>` 標籤缺少 `result` | API Server patch 未套用 | 執行上方的 patch 指南 |
| 工具卡片顯示但結果空白 | API Server `result` 欄位缺失 | 檢查 `grep '"result":'` 在 api_server.py |
| 模型輸出 `<details>` 標籤 | 污染反饋迴圈 | 確認 `enable_history_sanitization: true` |
| Responses API 中模型忘記工具結果 | 有狀態模式未注入 | 檢查 filter 日誌中的 `[responses] Injected` |
| 代理無回應 | 服務當機 | `journalctl -u hermes-tool-filter` 查看日誌 |
| 路由到錯誤的上游 | 路徑不匹配 | 確認路由表與 Gateway 端口一致 |
| `hermes update` 後失效 | Patch 被 git reset 覆蓋 | 更新後重新套用 patch |

---

## 技術細節

- **依賴** — FastAPI, aiohttp, PyYAML
- **架構** — 單檔案，無外部資料庫，零狀態
- **部署** — systemd 或直接執行

---

<p align="center">
  <sub>MIT License</sub>
</p>
