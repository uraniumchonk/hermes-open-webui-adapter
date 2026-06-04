# Hermes Tool Filter

**Transparent SSE proxy that makes Hermes tool calls render correctly on any client**

<p align="center">
  English · <a href="README.md">繁體中文</a>
</p>

<p align="center">
  <a href="https://github.com/fastapi/fastapi"><img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python&logoColor=white"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square"></a>
</p>

---

## Why This Exists

Hermes Gateway embeds `<details>` HTML tags in SSE streams to display tool call status. But:

- **Conduit APP** — cannot render `<details>`, shows raw HTML
- **Open WebUI** — tool cards stay stale, never update to "completed"

Hermes Tool Filter sits between them, transforming the stream in real-time:

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
pip install -r requirements.txt
python main.py
```

Service starts on `http://0.0.0.0:9099`.

Set your Conduit APP or Open WebUI API Base URL to:

```
http://127.0.0.1:9099/30000/v1
```

---

## Configuration

Edit `config.yaml` to adjust settings:

- **tool_mode** — `passthrough` (pass-through) / `enhance` (enhanced, default) / `strip` (remove)
- **auto_split_threshold** — auto-split stream at N characters (`0` = disabled)
- **bind_host / bind_port** — listen address and port

Environment variables override `config.yaml` (`TOOL_MODE`, `BIND_PORT`, `BIND_HOST`, `AUTO_SPLIT_THRESHOLD`).

### Routing Table

| Path | Upstream | Profile |
|------|----------|---------|
| `/30000/v1/*` | `127.0.0.1:30000` | Default |
| `/30001/v1/*` | `127.0.0.1:30001` | Coder |
| `/30002/v1/*` | `127.0.0.1:30002` | Analyst |
| `/30003/v1/*` | `127.0.0.1:30003` | Trader |

---

## How It Works

```
Conduit / Open WebUI ──▶ Hermes Tool Filter ──▶ Hermes Gateway
     (30010)                (9099)                  (30000)
        ◀──── Transformed SSE ◀────────────────────────────
```

1. Client sends request to proxy (Port 9099)
2. Proxy routes to the matched Hermes Gateway by path prefix
3. Gateway returns SSE stream with `<details>` tags
4. Proxy parses and converts to Markdown on-the-fly
5. Clean stream returned to client

---

## Features

- **SSE Stream Transformation** — `<details>` HTML → plain Markdown
- **Multi-Tenant Routing** — one proxy, multiple Gateway profiles
- **Three Display Modes** — passthrough, enhance, strip, switched via config
- **Tool Formatting** — `**💻 terminal** 🔄 \`echo hello\``
- **config.yaml Configuration** — centralized, no code changes needed

---

## As Systemd Service

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

## Technical Details

- **Dependencies** — FastAPI, aiohttp, PyYAML
- **Architecture** — single file, no database, stateless
- **Deployment** — systemd or direct execution

---

<p align="center">
  <sub>MIT License</sub>
</p>
