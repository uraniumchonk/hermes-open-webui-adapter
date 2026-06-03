# Hermes Open WebUI Adapter

透過過濾 Hermes Gateway 自訂 Tools 格式，在轉發過程中讓 **Open WebUI** 與 **Conduit APP** 能正確渲染工具卡片。

---

## 📋 目錄

- [專案概述](#-專案概述)
- [架構與資料流程](#-架構與資料流程)
- [功能特性](#-功能特性)
- [安裝步驟](#-安裝步驟)
- [配置方法](#-配置方法)
- [使用方式](#-使用方式)
- [環境變數](#-環境變數)
- [路徑路由](#-路徑路由)
- [工具格式化](#-工具格式化)
- [自動分割](#-自動分割)
- [系統服務](#-系統服務)
- [疑難排解](#-疑難排解)

---

## 📖 專案概述

Hermes Gateway 在 SSE (Server-Sent Events) 串流中會嵌入 `<details>` HTML 標籤來顯示工具調用狀態，但：

| 客戶端 | 問題 |
|--------|------|
| **Conduit APP** | 無法渲染 `<details>` 標籤，顯示原始 HTML 原始碼 |
| **Open WebUI** | 工具卡片狀態殘留，無法正確更新為完成 |

此代理伺服器作為中介層，即時轉換 SSE 串流中的工具資訊格式，確保各客戶端都能正確顯示。

**Before (原始格式):**
```html
<details type="tool_calls" done="false" id="tc-001" name="terminal">
<summary>💻 Running... echo hello</summary>
</details>
```

**After (轉換後格式):**
```markdown
**💻 terminal** 🔄 `echo hello`
```

---

## 🏗️ 架構與資料流程

```
┌─────────────┐         ┌──────────────────────┐         ┌────────────────┐
│  Open WebUI  │────────▶│  Hermes Tool Filter   │────────▶│  Hermes Gateway  │
│  (Port 30010)  │         │  (Port 9099)         │         │  (Port 30000)    │
└───────┬────────┘         └──────────┬───────────┘         └────────┬─────────┘
        │                             │                              │
        │              ④ 轉換後 SSE ◀─┘                              │
        │                             │                              │
        │              ① 請求        │                              │
        │────────────────────────────▶│                              │
        │                             │              ② 轉發          │
        │                             │─────────────────────────────▶│
        │                             │                              │
        │                             │              ③ 原始 SSE      │
        │                             │◀─────────────────────────────┘
        │                             │
        │                    SSE 轉換處理：
        │                    ─────────────────
        │                     1. 接收原始 SSE
        │                     2. 解析 hermes.tool.progress
        │                     3. 提取工具名稱 + 參數
        │                     4. 格式化為 Markdown
        │                     5. 回傳給客戶端
        │                              │
        │                    ┌─────────────────┐
        │                    │  Before (原始)   │
        │                    │ <details> HTML   │
        │                    └────────┬────────┘
        │                    ┌────────▼────────┐
        │                    │  After (轉換)    │
        │                    │ 純文字 Markdown  │
        │                    └─────────────────┘
        └──────────────────────────────────────┘
```

---

## ✨ 功能特性

| 功能 | 說明 |
|------|------|
| **SSE 串流轉換** | 即時轉換 SSE 串流中的 `<details>` HTML 標籤為純文字 Markdown |
| **多租戶路由** | 支援多個 Hermes Gateway profiles (30000-30003) |
| **智能客戶端偵測** | 根據 User-Agent 自動判斷是否轉換 (Conduit / Open WebUI) |
| **工具格式化** | 將工具調用格式化為 `**💻 terminal** 🔄 \`echo hello\`` 格式 |
| **自動會話分割** | 當輸出超過閾值時自動分割會話，防止串流過長導致錯誤 |
| **Emoji 映射** | 為每個工具自動分配對應的 emoji 標記 |
| **參數截斷** | 過長的參數自動截斷（預設 50 字元） |

---

## 🚀 安裝步驟

### 1. 克隆專案

```bash
git clone https://github.com/Thisisawa/hermes-open-webui-adapter.git
cd hermes-open-webui-adapter
```

### 2. 安裝依賴

```bash
pip install fastapi uvicorn httpx
```

### 3. 設定環境變數（可選）

建立 `.env` 檔案：

```bash
AUTO_SPLIT_THRESHOLD=4000
```

### 4. 啟動服務

```bash
python main.py
```

服務將啟動在 `http://0.0.0.0:9099`

---

## ⚙️ 配置方法

### 埠號路由表

預設路由表位於 `main.py` 的 `PORT_MAP`：

| 路徑前綴 | 上游位址 | 說明 |
|----------|---------|------|
| `/30000/v1/*` | `http://127.0.0.1:30000` | Default profile |
| `/30001/v1/*` | `http://127.0.0.1:30001` | Coder profile |
| `/30002/v1/*` | `http://127.0.0.1:30002` | Analyst profile |
| `/30003/v1/*` | `http://127.0.0.1:30003` | Trader profile |

**修改路由表：** 編輯 `main.py` 中的 `PORT_MAP` 字典。

### 繫結位址

```python
BIND_HOST = "0.0.0.0"  # 監聽位址
BIND_PORT = 9099        # 監聽埠號
```

### 自動分割閾值

透過環境變數 `AUTO_SPLIT_THRESHOLD` 控制（預設 4000 字元）：

```bash
# 關閉自動分割
AUTO_SPLIT_THRESHOLD=0 python main.py

# 設定為 8000 字元
AUTO_SPLIT_THRESHOLD=8000 python main.py
```

### 工具 Emoji 映射

```python
TOOL_EMOJI = {
    "terminal": "💻",
    "read_file": "📖",
    "write_file": "✍️",
    "patch": "🩹",
    "search_files": "🔎",
    "execute_code": "🐍",
    "delegate_task": "🔀",
    "clarify": "❓",
    "todo": "📋",
    "web_search": "🌐",
    "memory": "🧠",
    "skill_view": "🛠️",
    "session_search": "🔍",
    "process": "⚙️",
}
DEFAULT_EMOJI = "🔧"  # 未映射的工具使用此 emoji
```

---

## 🔌 使用方式

### Open WebUI 配置

在 Open WebUI 的 **Chat** 設定中：

| 設定項目 | 值 |
|----------|-----|
| **API Base URL** | `http://127.0.0.1:9099/30000/v1` |
| **API Key** | 你的 Hermes Gateway API Key |
| **Model** | `hermes-agent` |

### Conduit APP 配置

在 Conduit APP 的 API 設定中：

| 設定項目 | 值 |
|----------|-----|
| **API Base URL** | `http://127.0.0.1:9099/30000/v1` |
| **API Key** | 你的 Hermes Gateway API Key |

### 直接呼叫範例

```bash
# 透過代理呼叫
curl -X POST http://127.0.0.1:9099/30000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hermes-agent",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'

# 直接呼叫 Gateway (無轉換)
curl -X POST http://127.0.0.1:30000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hermes-agent",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

---

## 🔧 環境變數

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `AUTO_SPLIT_THRESHOLD` | `4000` | 自動分割閾值（字元數），設為 `0` 關閉 |

---

## 📂 路徑路由

### 有埠號前綴

```
/30000/v1/chat/completions  →  http://127.0.0.1:30000/v1/chat/completions
/30001/v1/chat/completions  →  http://127.0.0.1:30001/v1/chat/completions
/30000/v1/models            →  http://127.0.0.1:30000/v1/models
```

### 無埠號前綴（預設）

```
/v1/chat/completions  →  http://127.0.0.1:30000/v1/chat/completions
/v1/models            →  http://127.0.0.1:30000/v1/models
```

---

## 🎨 工具格式化

### 轉換前（原始格式）

```html
<details type="tool_calls" done="false" id="tc-001" name="terminal">
<summary>💻 Running... echo hello</summary>
</details>
```

### 轉換後（Markdown 格式）

```markdown
**💻 terminal** 🔄 `echo hello`
**📖 read_file** ✅ `main.py`
**🔎 search_files** ✅ `*.py`
```

### 格式化規則

| 元素 | 格式 |
|------|------|
| 工具名稱 | `**emoji 名稱**` (粗體) |
| 狀態 | `🔄` (執行中) / `✅` (已完成) |
| 參數 | `` `參數` `` (inline code) |
| 長參數 | 超過 50 字元自動截斷並加 `...` |

---

## ✂️ 自動分割

當 SSE 串流累積超過 `AUTO_SPLIT_THRESHOLD` 字元時，會自動：

1. 發送 `finish_reason: "length"` 結束當前串流
2. 發送 `[DONE]` 標記
3. 發送 `session.split` 事件
4. 清空計數器繼續處理

**適用場景：** 防止 Conduit APP 或 Open WebUI 因串流過長而出現 `TransferEncodingError`。

---

## 🖥️ 系統服務

### Systemd 服務檔

```ini
[Unit]
Description=Hermes Tool Card Enhancer Proxy
After=network-online.target

[Service]
Type=simple
User=user
WorkingDirectory=/home/user/hermes_tool_filter
ExecStart=/opt/hermes/hermes-agent/venv/bin/python /home/user/hermes_tool_filter/main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 服務管理

```bash
# 啟動
sudo systemctl start hermes-tool-filter

# 停止
sudo systemctl stop hermes-tool-filter

# 重新啟動
sudo systemctl restart hermes-tool-filter

# 查看狀態
sudo systemctl status hermes-tool-filter

# 查看日誌
sudo journalctl -u hermes-tool-filter -f

# 設定開機啟動
sudo systemctl enable hermes-tool-filter
```

---

## 🐛 疑難排解

### 埠號衝突

如果啟動時出現埠號佔用錯誤：

```bash
# 查看佔用埠號的程序
fuser 9099/tcp

# 強制結束
fuser -k 9099/tcp
```

### 401 認證失敗

確認 API Key 正確：

```bash
# 檢查 .env 檔案中的 key
cat .env
```

### 串流格式問題

如果工具顯示不正常，檢查：

1. 服務是否正常運行：`curl http://127.0.0.1:9099/health`
2. User-Agent 是否正確被偵測
3. 上游 Gateway 是否正常：`curl http://127.0.0.1:30000/v1/models`

---

## 📁 專案結構

```
hermes-open-webui-adapter/
├── main.py              # 核心代理程式
├── requirements.txt     # Python 依賴
├── .gitignore           # Git 忽略檔案
├── README.md            # 說明文件
└── architecture.excalidraw  # 架構圖 (Excalidraw 格式)
```

---

## 📜 License

MIT

---

## 🙏 致謝

- [Hermes Agent](https://github.com/nousresearch/hermes) - 強大的 AI 代理框架
- [Open WebUI](https://github.com/open-webui/open-webui) - 開源聊天介面
