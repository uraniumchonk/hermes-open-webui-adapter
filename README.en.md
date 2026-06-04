# Hermes Open WebUI Adapter

<p align="center">
  English · <a href="README.md">繁體中文</a>
</p>

<p align="center">
  <b>Make Hermes tool calls render correctly on any client</b>
  <br>
  Transparent SSE proxy that transforms Hermes Gateway's <code>&lt;details&gt;</code> HTML tags into clean Markdown
</p>

<p align="center">
  <a href="https://github.com/fastapi/fastapi"><img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python&logoColor=white"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square"></a>
</p>

---

## Why This Exists

Hermes Gateway embeds `<details>` HTML tags in SSE streams to show tool call status. But:

- **Conduit APP** → cannot render `<details>`, shows raw HTML
- **Open WebUI** → tool cards stay stale, never update to "completed"

This proxy sits between them, transforming the stream in real-time:

```
<details type="tool_calls" done="false" name="terminal">
<summary>💻 Running... echo hello</summary>
</details>

        ↓ transformed ↓

**💻 terminal** 🔄 `echo hello`
```

---

## Quick Start

```bash
git clone https://github.com/Thisisawa/hermes-open-webui-adapter.git
cd hermes-open-webui-adapter
pip install fastapi uvicorn httpx
python main.py
```

Service starts on `http://0.0.0.0:9099`.

---

## Architecture

```
Open WebUI / Conduit ──▶ Hermes Tool Filter ──▶ Hermes Gateway
     (30010)                (9099)                  (30000)
        ◀──── Transformed SSE ◀────────────────────────────
```

1. Client sends request to proxy (Port 9099)
2. Proxy forwards to the matched Hermes Gateway
3. Gateway returns SSE stream with `<details>` tags
4. Proxy parses and converts to Markdown on-the-fly
5. Clean stream returned to client

---

## Configuration

### Routing Table

Edit `PORT_MAP` in `main.py`:

| Path | Upstream | Profile |
|------|----------|---------|
| `/30000/v1/*` | `127.0.0.1:30000` | Default |
| `/30001/v1/*` | `127.0.0.1:30001` | Coder |
| `/30002/v1/*` | `127.0.0.1:30002` | Analyst |
| `/30003/v1/*` | `127.0.0.1:30003` | Trader |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTO_SPLIT_THRESHOLD` | `4000` | Auto-split stream at N chars (`0` = disabled) |

### Client Setup

Set API Base URL in Open WebUI or Conduit APP to:

```
http://127.0.0.1:9099/30000/v1
```

---

## Features

- **SSE Stream Transformation** — `<details>` HTML → plain Markdown
- **Multi-Tenant Routing** — one proxy, multiple Gateway profiles
- **Smart Client Detection** — auto-detects via User-Agent
- **Tool Formatting** — `**💻 terminal** 🔄 \`echo hello\``
- **Auto Session Split** — prevents `TransferEncodingError` on long streams
- **Parameter Truncation** — caps at 50 chars

---

## As Systemd Service

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
