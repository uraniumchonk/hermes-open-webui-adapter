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

Hermes Gateway 的 SSE 串流使用自訂的 `<details>` HTML 標籤格式來顯示工具調用狀態，但這個格式與 Open WebUI / Conduit APP 期望的格式不符，導致**工具卡片完全不顯示**。

Hermes Tool Filter 在兩者之間做即時轉換，將 Hermes 的格式轉為客戶端能正確渲染的格式：

**passthrough 模式**（預設）：直接透傳，適合已支援 Hermes 格式的客戶端。

**enhance 模式**（推薦）：過濾掉中間狀態的標籤，只在工具完成時注入一個符合標準的 `done="true"` 標籤，包含完整的工具名稱、參數和結果：

```html
<!-- Hermes 原始輸出：格式不符，客戶端無法渲染 -->

<!-- 經過 enhance 模式轉換後： -->
<details type="tool_calls" done="true" name="terminal" arguments="{&quot;input&quot;: &quot;echo hello&quot;}">
<summary>Done</summary>
</details>
```

**strip 模式**：移除 `<details>` 並替換為純文字 Markdown（舊版兼容）。

---

## 快速開始

```bash
git clone https://github.com/Thisisawa/hermes-open-webui-adapter.git
cd hermes-open-webui-adapter
pip install -r requirements.txt
python main.py
```

服務啟動在 `http://0.0.0.0:9099`

將 Conduit APP 或 Open WebUI 的 API Base URL 設為：

```
http://127.0.0.1:9099/30000/v1
```

---

## 配置

編輯 `config.yaml` 即可調整設定：

- **tool_mode** — `passthrough`（透傳）/ `enhance`（增強，預設）/ `strip`（移除）
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
Conduit / Open WebUI ──▶ Hermes Tool Filter ──▶ Hermes Gateway
     (30010)                (9099)                  (30000)
        ◀──────── 轉換後 SSE ◀────────────────────────────
```

1. 客戶端發送請求到代理（Port 9099）
2. 代理根據路徑轉發到對應的 Hermes Gateway
3. Gateway 回傳 SSE 串流（含 `<details>` 標籤）
4. 代理即時解析並轉換為 Markdown
5. 回傳乾淨的串流給客戶端

---

## 功能

- **格式轉換** — 將 Hermes 自訂格式轉為客戶端可渲染的標準格式
- **三種處理模式** — passthrough（透傳）、enhance（增強）、strip（純文字）
- **多租戶路由** — 一個代理對應多個 Gateway profiles
- **智能過濾** — enhance 模式自動過濾中間狀態，只輸出完成標籤
- **完整工具資訊** — 注入的標籤包含工具名稱、參數、結果
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

## 技術細節

- **依賴** — FastAPI, aiohttp, PyYAML
- **架構** — 單檔案，無外部資料庫，零狀態
- **部署** — systemd 或直接執行

---

<p align="center">
  <sub>MIT License</sub>
</p>
