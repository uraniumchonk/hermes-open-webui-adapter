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

Hermes Gateway uses a custom `<details>` HTML tag format in its SSE stream to display tool call status, but this format does not match what Open WebUI / Conduit APP expects — resulting in **tool cards not rendering at all**.

Hermes Tool Filter sits between them, transforming the stream in real-time to a format the client can render correctly:

**passthrough mode** (default): direct pass-through for clients that support Hermes format natively.

**enhance mode** (recommended): filter out intermediate state tags, inject a single standards-compliant `done="true"` tag on completion with full tool name, arguments, and result:

```html
<!-- Raw Hermes output: format mismatch, client cannot render -->

<!-- After enhance mode transformation: -->
<details type="tool_calls" done="true" name="terminal" arguments="{&quot;input&quot;: &quot;echo hello&quot;}">
<summary>Done</summary>
</details>
```

**strip mode**: remove `<details>` and replace with plain Markdown (legacy compatibility).

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

- **Format Transformation** — converts Hermes custom format to client-renderable standard format
- **Three Processing Modes** — passthrough, enhance, strip
- **Multi-Tenant Routing** — one proxy, multiple Gateway profiles
- **Smart Filtering** — enhance mode filters intermediate states, outputs only completion tags
- **Complete Tool Info** — injected tags include name, arguments, result
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
