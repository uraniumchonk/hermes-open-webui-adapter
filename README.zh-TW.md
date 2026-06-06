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

## 為什麼需要它？

Hermes Gateway 的 SSE 串流使用自訂的 `<details>` HTML 標籤格式來顯示工具調用狀態，但這個格式與 Open WebUI / Conduit APP 期望的格式不符，導致**工具卡片完全不顯示**且**工具結果丟失在對話上下文中**。

Hermes Tool Filter 在兩者之間做即時轉換，將 Hermes 的格式轉為客戶端能正確渲染的格式：

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

```
Open WebUI ──▶ Hermes Tool Filter ──▶ Hermes Gateway
   (30010)            (9099)                 (30000)
      ◀──────── 轉換後 SSE ◀────────────────────────────
```

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
