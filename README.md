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

> **Solves**: Hermes Gateway tool cards not rendering in Open WebUI / Conduit APP, and tool results being lost from conversation context — causing model amnesia.

---

## 🌟 Highlights

- ⚡ **enhance-v2 mode** — Real-time streaming + complete `<details>` child-element format, fully compatible with OpenWebUI
- 🏢 **Multi-profile routing** — One proxy for multiple Gateway profiles (Chatting / Coder / Analyst / Trader)
- 🧹 **Smart filtering** — Auto-filters intermediate states, outputs only completion tags
- 🔧 **One-click patch** — Includes auto-apply patch + verification script for Hermes API Server
- 📦 **Zero state** — Single file, no database, config-driven via `config.yaml`

---

## Table of Contents

- [The Problem: Tool Context Loss](#the-problem-tool-context-loss)
- [The Solution](#the-solution)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [How It Works](#how-it-works)
- [Features](#features)
- [Systemd Service](#systemd-service)
- [🔧 Hermes API Server Patch Guide](#-hermes-api-server-patch-guide)
- [Troubleshooting](#troubleshooting)
- [Technical Details](#technical-details)

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

- `05:46:12 → 05:46:27`: prompt_tokens jumped from **18,301 → 21,240** (+2,939 tokens during tool loop)
- `05:46:45` (new message after tool loop): prompt_tokens dropped to **18,698** — only +397 tokens over original
- **Expected**: ~21,240+ tokens if tool results were properly included
- **Actual**: 18,698 — tool results were **NOT** in the conversation history

This proves Open WebUI reconstructed the conversation **without tool results**, causing the model to lose all context from tool execution.

### Root Cause: A Broken Chain

```
Hermes Gateway API Server
  │
  │ ✗ Problem 1: Never emits standard OpenAI tool_calls delta
  │             Only sends custom hermes.tool.progress SSE events
  │ ✗ Problem 2: completed event does NOT include the tool result
  │             (function_result exists but is never added to payload)
  │
  ▼
hermes_tool_filter
  │
  │ ✗ Problem 3: enhance-v2 mode was unimplemented (fell back to passthrough)
  │ ✗ Problem 4: Even enhance mode couldn't get results (due to Problem 2)
  │             final_result = parsed_json.get("result", "") ← always empty
  │
  ▼
Open WebUI
  │
  │ ✗ Problem 5: Receives assistant message with only final text
  │             Tool information never enters conversation history
  │ ✗ Problem 6: Unpaired tool_use/tool_result messages are discarded
  │
  ▼
Next request
  │
  │ ✗ Problem 7: Open WebUI sends back messages WITHOUT tool results
  │             Model has zero visibility into previous tool execution
  │
  ▼
🧠💥 Model amnesia!
```

### Detailed Breakdown

**Layer 1 — Hermes Gateway API Server** (`gateway/platforms/api_server.py`):

The API server runs a full agent loop internally (correctly maintaining conversation history with tool results). However, its SSE output has two critical gaps:

1. **No standard `tool_calls` delta**: It doesn't emit `delta.tool_calls` or `role: "tool"` messages — only custom `hermes.tool.progress` events.
2. **Missing result in completed event**: The `_on_tool_complete` callback receives `function_result` but never includes it:
   ```python
   ("__tool_progress__", {
       "tool": function_name,
       "toolCallId": tool_call_id,
       "status": "completed",       # ← no "result" field!
   })
   ```

**Layer 2 — hermes_tool_filter** (`main.py`):

Configured as `enhance-v2` but code only checked `TOOL_MODE == "enhance"`, so it was effectively passthrough. Even in enhance mode, `result` was always empty.

**Layer 3 — Open WebUI**:

Receives an assistant message with only the final response text. Without standard tool messages in the stream, nothing gets stored. When the user sends a new message, the reconstructed history has no tool context.

**✅ Verification: Hermes Gateway internally is correct.** From `kv_cache.log`:
```
19:26:01 | prompt_tokens=32833 | msgs=14 | tools_def=26
  system: 1 messages, 23897 chars
  user: 1 messages, 1282 chars
  assistant: 6 messages, 356 chars
  tool: 6 messages, 37033 chars  ← tool results ARE present internally
```

The issue is in the **SSE transformation pipeline**, not in Hermes itself.

---

## The Solution

Hermes Tool Filter bridges the gap by converting Hermes' custom SSE format into a format Open WebUI can parse, render, and persist.

**enhance-v2 mode** (default, recommended):

```html
<!-- After enhance-v2 transformation: -->
<details type="tool_calls" done="true" name="web_search">
<summary>Done</summary>
<arguments>Taipei weather today</arguments>
<result>{"success":true,"data":"..."}</result>
</details>
```

**enhance mode**: Filters intermediate states, injects `done="true"` tag on completion.

**passthrough mode**: Direct passthrough for Hermes-compatible clients.

**strip mode**: Removes `<details>`, replaces with plain Markdown (legacy).

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

> **Note**: metrics_proxy is NOT in the data path — it monitors a separate vLLM port.

### Request Flow

1. **Open WebUI** → `hermes_tool_filter` (port 9099)
2. **Proxy** → **Hermes Gateway** (port 30000+)
3. **Gateway** executes tool loop (full history maintained internally)
4. **Gateway** → SSE stream → **Proxy**
5. **Proxy** transforms format → **Open WebUI**
6. **Open WebUI** stores to local database
7. User sends new message → history reconstructed with tool context

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

Edit `config.yaml`:

- **tool_mode** — `enhance-v2` (default, recommended) / `enhance` / `passthrough` / `strip`
- **auto_split_threshold** — Stream auto-split threshold (characters, `0` = disabled)
- **bind_host / bind_port** — Listen address and port

Environment variables override config.yaml (`TOOL_MODE`, `BIND_PORT`, `BIND_HOST`, `AUTO_SPLIT_THRESHOLD`).

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

- 🔄 **Format conversion** — Hermes custom format → standard client-renderable format
- 🎛️ **Four processing modes** — enhance-v2 (default), enhance, passthrough, strip
- 🏢 **Multi-tenant routing** — One proxy, multiple Gateway profiles
- 🧠 **Smart filtering** — Auto-filters hermes.tool.progress events, outputs only completion tags
- 📋 **Complete tool info** — Tool name, arguments, and results (child element format)
- ⚙️ **Config-driven** — Centralized management, no code changes needed

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

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Tool cards don't show in Open WebUI | enhance-v2 not active | Verify `tool_mode: "enhance-v2"` in config.yaml |
| `<details>` tags missing `result` | API Server patch not applied | Run the patch guide above |
| Tool cards show but result is empty | API Server `result` field missing | Check `grep '"result":'` in api_server.py |
| Proxy not responding | Service crashed | `journalctl -u hermes-tool-filter` or check logs |
| Wrong upstream | Wrong route path | Verify routing table matches your Gateway ports |
| `hermes update` broke things | Patch overwritten by git reset | Re-apply patch after update |

---

## Technical Details

- **Dependencies** — FastAPI, aiohttp, PyYAML
- **Architecture** — Single file, no external database, stateless
- **Deployment** — systemd or run directly

---

<p align="center">
  <sub>MIT License</sub>
</p>
