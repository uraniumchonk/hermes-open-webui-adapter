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

## The Problem: Tool Context Loss in Open WebUI

When using **Hermes Agent as a server-side tool executor** through Open WebUI, the model experiences **complete amnesia** — it forgets tool execution results after the tool loop finishes. The model cannot reference previous tool outputs, leading to confused behavior and errors.

### Evidence: Token Count Analysis

Data captured from `metrics_proxy` shows the issue clearly:

```
Time         Model              Input    Output  TTFT      TG/s    Duration
05:46:45    qwen-27b-default   18698    396     1,578ms   37.8    12,062ms
05:46:27    qwen-27b-default   21240    384     2,258ms   41.2    11,568ms
05:46:15    qwen-27b-default   19576    72      2,045ms   55.1    3,353ms
05:46:12    qwen-27b-default   18301    57      1,195ms   61.0    2,130ms
```

**Key findings:**
- `05:46:12 → 05:46:27`: prompt_tokens increased from 18,301 to 21,240 (**+2,939 tokens** during tool loop)
- `05:46:45` (new message after tool loop): prompt_tokens is only 18,698 — just **+397 tokens** over the original
- **If tool results were properly included, 05:46:45 should have ~21,240+ tokens**
- **Actual: only 18,698 — tool results were NOT in the conversation history**

This proves Open WebUI reconstructed the conversation **without tool results**, causing the model to lose all context from tool execution.

### Root Cause: A Broken Chain

The problem is a chain of failures across three layers:

```
Hermes Gateway API Server
  │
  │ Problem 1: Never emits standard OpenAI tool_calls delta format
  │             Only sends custom hermes.tool.progress SSE events
  │ Problem 2: completed event does NOT include the tool result
  │             (function_result exists but is never added to the payload)
  │
  ▼
hermes_tool_filter
  │
  │ Problem 3: enhance-v2 mode was unimplemented (always fell back to passthrough)
  │ Problem 4: Even enhance mode couldn't get results (due to Problem 2)
  │             final_result = parsed_json.get("result", "") ← always empty
  │
  ▼
Open WebUI
  │
  │ Problem 5: Receives assistant message with only final text
  │             Tool information never enters conversation history
  │ Problem 6: Unpaired tool_use/tool_result messages are discarded
  │
  ▼
Next request
  │
  │ Problem 7: Open WebUI sends back messages WITHOUT tool results
  │             Model has zero visibility into previous tool execution
  │
  ▼
Model amnesia! 🧠💥
```

### Detailed Breakdown

**Layer 1 — Hermes Gateway API Server** (`gateway/platforms/api_server.py`):

The API server runs a full agent loop internally (correctly maintaining conversation history with tool results). However, its SSE output has two critical gaps:

1. **No standard `tool_calls` delta**: It doesn't emit `delta.tool_calls` or `role: "tool"` messages — only custom `hermes.tool.progress` events.
2. **Missing result in completed event**: The `_on_tool_complete` callback receives `function_result` but never includes it in the SSE payload:
   ```python
   ("__tool_progress__", {
       "tool": function_name,
       "toolCallId": tool_call_id,
       "status": "completed",       # ← no "result" field!
   })
   ```

**Layer 2 — hermes_tool_filter** (`main.py`):

The filter was configured as `enhance-v2` but the code only checked `TOOL_MODE == "enhance"`, so it was effectively a passthrough. Even in enhance mode, the `result` field was always empty because the API Server didn't send it.

**Layer 3 — Open WebUI**:

Open WebUI receives an assistant message containing only the final response text. Without standard tool_calls/tool messages in the stream, it has nothing to store in its conversation database. When the user sends a new message, Open WebUI reconstructs the history without any tool context.

**Verification: Hermes Gateway internally is correct.** From `kv_cache.log`:
```
19:26:01 | prompt_tokens=32833 | msgs=14 | tools_def=26
  system: 1 messages, 23897 chars
  user: 1 messages, 1282 chars
  assistant: 6 messages, 356 chars
  tool: 6 messages, 37033 chars  ← tool results ARE present internally
```

This confirms the issue is in the **SSE transformation pipeline**, not in Hermes itself.

---

## The Solution

Hermes Tool Filter bridges the gap between Hermes Gateway and Open WebUI by converting Hermes' custom SSE format into a format Open WebUI can parse, render, and persist.

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

## Architecture

```
User Browser
    │
    ▼
Open WebUI (30010)
    │ POST http://127.0.0.1:9099/30001/v1/chat/completions
    ▼
hermes_tool_filter (9099)
    │ Mode: enhance-v2
    │ Converts hermes.tool.progress events → <details> HTML tags
    │ Routes: /30001/v1/* → http://127.0.0.1:30001/v1/*
    ▼
Hermes Gateway API Server (30001)
    │ aiohttp-based, OpenAI-compatible endpoint
    │ Runs full agent loop with tool execution
    ▼
vLLM (backend)
    │
    └── metrics_proxy (18080) monitors vLLM instances
```

> **Note**: metrics_proxy is NOT in the Hermes Gateway data path — it monitors a separate vLLM port.

### Request Flow

1. **Open WebUI** sends request to `hermes_tool_filter` (port 9099)
2. **hermes_tool_filter** forwards to **Hermes Gateway** (port 30000)
3. **Hermes Gateway** executes tool loop (maintains full conversation history internally)
4. **Hermes Gateway** returns SSE stream to **hermes_tool_filter**
5. **hermes_tool_filter** transforms SSE format and returns to **Open WebUI**
6. **Open WebUI** receives response and stores to local database
7. User sends new message → **Open WebUI reconstructs conversation history** and sends new request

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
- **Config-driven** — Centralized management via config.yaml, no code changes needed

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
