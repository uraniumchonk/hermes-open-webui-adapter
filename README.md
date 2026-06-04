# Hermes Open WebUI Adapter

<p align="center">
  <a href="README.en.md">English</a> · 繁體中文
</p>

<p align="center">
  <b>讓 Hermes 工具調用在任何客戶端都能正確顯示</b>
  <br>
  透明的 SSE 代理，即時轉換 Hermes Gateway 的 <code>&lt;details&gt;</code> HTML 標籤為純文字 Markdown
</p>

<p align="center">
  <a href="https://github.com/fastapi/fastapi"><img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python&logoColor=white"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square"></a>
</p>

---

## 為什麼需要這個？

Hermes Gateway 的 SSE 串流會嵌入 `<details>` HTML 標籤來顯示工具狀態。但：

- **Conduit APP** → 無法渲染，顯示原始 HTML 碼
- **Open WebUI** → 工具卡片殘留，狀態不更新

這個代理在兩者之間做即時轉換，讓工具調用變成乾淨的 Markdown：

```
<details type="tool_calls" done="false" name="terminal">
<summary>💻 Running... echo hello</summary>
</details>

        ↓ 轉換後 ↓

**💻 terminal** 🔄 `echo hello`
```

---

## 快速開始

```bash
git clone https://github.com/Thisisawa/hermes-open-webui-adapter.git
cd hermes-open-webui-adapter
pip install fastapi uvicorn httpx
python main.py
```

服務啟動在 `http://0.0.0.0:9099`

---

## 架構

```
Open WebUI / Conduit ──▶ Hermes Tool Filter ──▶ Hermes Gateway
     (30010)                (9099)                  (30000)
        ◀──────── 轉換後 SSE ◀────────────────────────────
```

1. 客戶端發送請求到代理（Port 9099）
2. 代理轉發到對應的 Hermes Gateway
3. Gateway 回傳 SSE 串流（含 `<details>` 標籤）
4. 代理即時解析並轉換為 Markdown
5. 回傳乾淨的串流給客戶端

---

## 配置

### 路由表

編輯 `main.py` 中的 `PORT_MAP`：

| 路徑 | 上游 | 用途 |
|------|------|------|
| `/30000/v1/*` | `127.0.0.1:30000` | Default |
| `/30001/v1/*` | `127.0.0.1:30001` | Coder |
| `/30002/v1/*` | `127.0.0.1:30002` | Analyst |
| `/30003/v1/*` | `127.0.0.1:30003` | Trader |

### 環境變數

| 變數 | 預設 | 說明 |
|------|------|------|
| `AUTO_SPLIT_THRESHOLD` | `4000` | 串流超過此字元數自動分割（`0` = 關閉） |

### Open WebUI / Conduit 設定

將 API Base URL 設為：`http://127.0.0.1:9099/30000/v1`

---

## 功能

- **SSE 串流轉換** — `<details>` HTML → 純文字 Markdown
- **多租戶路由** — 一個代理對應多個 Gateway profiles
- **智能客戶端偵測** — 根據 User-Agent 自動判斷是否轉換
- **工具格式化** — `**💻 terminal** 🔄 \`echo hello\``
- **自動會話分割** — 防止長串流導致 `TransferEncodingError`
- **參數截斷** — 超過 50 字元自動截斷

---

## Systemd 服務

```ini
[Unit]
Description=Hermes Tool Card Enhancer Proxy
After=network-online.target

[Service]
Type=simple
User=YOUE_USER
WorkingDirectory=PATH_TO/hermes_tool_filter
ExecStart=YOUE_VENE PATH_TO/hermes_tool_filter/main.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now hermes-tool-filter
```

---

<p align="center">
  <sub>MIT License</sub>
</p>
