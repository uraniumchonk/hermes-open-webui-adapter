# Hermes Tool Filter

**Transparent SSE proxy that makes Hermes tool calls render correctly in any client**

<p align="center">
  English · <a href="README.zh-TW.md">繁體中文</a>
</p>

<p align="center">
  <a href="https://github.com/fastapi/fastapi"><img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python&logoColor=white"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square"></a>
</p>

---

## Why do you need it?

Hermes Gateway's SSE stream uses custom `<details>` HTML tags to display tool call status, but this format doesn't match what Open WebUI / Conduit APP expects, resulting in **tool cards not showing at all** and **tool results lost from conversation context**.

Hermes Tool Filter sits between them and converts Hermes' format to what clients can render properly:

**enhance-v2 mode** (default, recommended): Real-time streaming + `<details>` child element format, fully compatible with OpenWebUI:

```html
<!-- After enhance-v2 transformation: -->
<details type="tool_calls" done="true" name="web_search">
<summary>Done</summary>
<arguments>Taipei weather today</arguments>
<result>{"success":true,"data":"..."}</result>
</details>
```

**enhance mode**: Filters out intermediate state tags, only injects a standard `done="true"` tag when a tool completes.

**passthrough mode**: Passes through directly, suitable for clients that already support Hermes format.

**strip mode**: Removes `<details>` and replaces with plain text Markdown (legacy compatibility).

---

## Quick Start

```bash
git clone https://github.com/uraniumchonk/hermes-open-webui-adapter.git
cd hermes-open-webui-adapter
pip install -r requirements.txt
python main.py
```

Service starts on `http://0.0.0.0:9099`

Set Open WebUI's API Base URL to:

```
http://127.0.0.1:9099/30000/v1
```

---

## Configuration

Edit `config.yaml` to adjust settings:

- **tool_mode** — `enhance-v2` (default, recommended) / `enhance` / `passthrough` / `strip`
- **auto_split_threshold** — Stream auto-split threshold (characters, `0` = disabled)
- **bind_host / bind_port** — Listen address and port

Environment variables can override config.yaml settings (`TOOL_MODE`, `BIND_PORT`, `BIND_HOST`, `AUTO_SPLIT_THRESHOLD`).

### Routing Table

| Path | Upstream | Purpose |
|------|------|------|
| `/30000/v1/*` | `127.0.0.1:30000` | Default |
| `/30001/v1/*` | `127.0.0.1:30001` | Coder |
| `/30002/v1/*` | `127.0.0.1:30002` | Analyst |
| `/30003/v1/*` | `127.0.0.1:30003` | Trader |

---

## How It Works

```
Open WebUI ──▶ Hermes Tool Filter ──▶ Hermes Gateway
   (30010)            (9099)                 (30000)
      ◀──── Transformed SSE ◀────────────────────────────
```

1. Client sends request to proxy (Port 9099)
2. Proxy forwards to the corresponding Hermes Gateway based on path
3. Gateway returns SSE stream (with `hermes.tool.progress` events)
4. Proxy parses and transforms to standard `<details>` format in real-time
5. Returns clean stream to client

---

## Features

- **Format conversion** — Converts Hermes' custom format to a standard format clients can render
- **Four processing modes** — enhance-v2 (default), enhance, passthrough, strip
- **Multi-tenant routing** — One proxy for multiple Gateway profiles
- **Smart filtering** — enhance-v2 automatically filters hermes.tool.progress events, only outputs completion tags
- **Complete tool info** — Injected tags include tool name, arguments, and results (child element format)
- **config.yaml configuration** — Centralized management, no code changes needed

---

## Systemd Service

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

## 🔧 Hermes API Server Patch Guide

> **Why is a patch needed?**
>
> Hermes Gateway's `_on_tool_complete` function doesn't send `arguments` and `result` fields in the `__tool_progress__` completed event, so hermes_tool_filter can't retrieve tool parameters and results to build `<details>` tags.

### Auto-Apply (Recommended)

```bash
# Check if patch is applied
grep -c '"result": str(function_result)' /opt/hermes/hermes-agent/gateway/platforms/api_server.py

# If it returns 0, it's not applied. Run:
cd ~/.hermes/hermes/hermes-agent
git apply /path/to/hermes_tool_filter/patches/api_server_tool_result.patch 2>/dev/null || \
  echo "Patch cannot be applied — may already be applied or Hermes version changed"
```

### Manual Apply

1. **Find `_on_tool_complete` function** (in `gateway/platforms/api_server.py`, around line 1858)

2. **Add `arguments` and `result` to `progress_data` dict**:

```python
async def _on_tool_complete(self, tool_call_id, function_name, function_args, function_result):
    # ... other code ...
    progress_data = {
        "type": "__tool_progress__",
        "event": "completed",
        "tool_call_id": tool_call_id,
        "name": function_name,
        "arguments": function_args or {},          # ← Add this
        "result": str(function_result) if function_result is not None else "",  # ← Add this
    }
```

3. **Restart Hermes Gateway**:

```bash
# Find and stop old process
ps aux | grep "hermes.*gateway"
kill <PID>

# Start new Gateway
python -m hermes_cli.main --profile chatting gateway run --replace
```

### Maintenance: After Hermes Update

`hermes update` (git pull/reset) will overwrite manual changes! After each Hermes update:

```bash
# Method 1: Use patch file
cd ~/.hermes/hermes/hermes-agent
git apply /path/to/hermes_tool_filter/patches/api_server_tool_result.patch

# Method 2: Manual check
grep '"result":' /opt/hermes/hermes-agent/gateway/platforms/api_server.py
# If not found, the patch was overwritten and needs to be re-applied
```

### Verify Patch is Active

```bash
# Test script
python3 /path/to/hermes_tool_filter/test_api_server.py

# Expected: completed event includes arguments and result fields
```

---

## Technical Details

- **Dependencies** — FastAPI, aiohttp, PyYAML
- **Architecture** — Single file, no external database, stateless
- **Deployment** — systemd or run directly

---

<p align="center">
  <sub>MIT License</sub>
</p>
