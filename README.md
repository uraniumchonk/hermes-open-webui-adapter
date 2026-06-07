# Hermes Tool Filter v2.0.0

**Transparent SSE proxy that makes Hermes tool calls render correctly in any client**

<p align="center">
  English · <a href="README.zh-TW.md">繁體中文</a>
</p>

<p align="center">
  <a href="https://github.com/fastapi/fastapi"><img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python&logoColor=white"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square"></a>
  <a href="#"><img src="https://img.shields.io/badge/Version-2.0.0-brightgreen?style=flat-square"></a>
</p>

> **Solves**: Hermes Gateway tool cards not rendering in Open WebUI / Conduit APP, and tool results being lost from conversation context — causing model amnesia.

---

## 🌟 Highlights

- ⚡ **enhance-v2 mode** — Real-time streaming + complete `<details>` child-element format, fully compatible with OpenWebUI
- 🏢 **Multi-profile routing** — One proxy for multiple Gateway profiles (Chatting / Coder / Analyst / Trader)
- 🧹 **History sanitization** — Anti-pollution: converts `<details>` tags in conversation history to natural language (19 templates), preventing the model from mimicking HTML output
- 📡 **Dual handler** — Separate handlers for `/v1/chat/completions` and `/v1/responses`, with automatic tool-result injection for stateful Responses API
- 🔧 **One-click patch** — Includes auto-apply patch + verification script for Hermes API Server
- 📦 **Zero state** — No database, config-driven via `config.yaml`

---

## Table of Contents

- [The Problem: Tool Context Loss](#the-problem-tool-context-loss)
- [The Solution](#the-solution)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [How It Works](#how-it-works)
- [Features](#features)
- [History Sanitization (Anti-Pollution)](#history-sanitization-anti-pollution)
- [Responses API Support](#responses-api-support)
- [Systemd Service](#systemd-service)
- [🔧 Hermes API Server Patch Guide](#-hermes-api-server-patch-guide)
- [Troubleshooting](#troubleshooting)
- [Technical Details](#technical-details)

---

## The Problem: Tool Context Loss in Open WebUI

When using **Hermes Agent as a server-side tool executor** through Open WebUI, the model experiences **complete amnesia** — it forgets tool execution results after the tool loop finishes. The model cannot reference previous tool outputs, leading to confused behavior and errors.

### Evidence: Token Count Discrepancy

When a tool loop runs, the prompt_tokens during the loop increases significantly (tool results are included internally). However, when the user sends a new message after the tool loop completes, the prompt_tokens drops back to near the original count — proving tool results were **not** persisted in the conversation history.

**Example:**

| Step | Description | prompt_tokens |
|------|-------------|---------------|
| 1 | Initial message | ~18,301 |
| 2 | Tool loop (internal) | ~21,240 (+2,939) |
| 3 | New message (after tool loop) | ~18,698 |

- **Expected at step 3**: ~21,240+ tokens (tool results in history)
- **Actual at step 3**: ~18,698 — tool results were **NOT** in the conversation history

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

**✅ Verification: Hermes Gateway internally is correct.** Hermes' internal KV cache confirms tool messages and results are properly maintained during the agent loop — the issue is purely in the SSE output pipeline.

The issue is in the **SSE transformation pipeline**, not in Hermes itself.

---

## The Solution

Hermes Tool Filter bridges the gap by converting Hermes' custom SSE format into a format Open WebUI can parse, render, and persist.

**enhance-v2 mode** (default, recommended):

```html
<!-- After enhance-v2 transformation: -->
<details type="tool_calls" done="true" name="terminal">
<summary>✅ 💻 echo hello</summary>
<arguments>{"tool_name": "terminal", "command": "echo hello"}</arguments>
<result>{"output": "hello", "exit_code": 0, "error": null}</result>
</details>
```

- `<summary>` — Tool name + emoji for visual identification
- `<arguments>` — Full JSON with `tool_name` + parameters (model can identify the tool)
- `<result>` — Tool execution output

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
    │ POST http://127.0.0.1:9099/30001/v1/responses
    ▼
hermes_tool_filter (9099)
    │ completions_handler.py  ← /v1/chat/completions (enhance-v2 + sanitization)
    │ responses_handler.py    ← /v1/responses (tool injection + SSE passthrough)
    │ Routes: /30001/v1/* → http://127.0.0.1:30001/v1/*
    ▼
Hermes Gateway API Server (30001)
    │ aiohttp-based, OpenAI-compatible endpoint
    │ Runs full agent loop with tool execution
    ▼
vLLM (backend)
```

### Request Flow

1. **Open WebUI** → `hermes_tool_filter` (port 9099)
2. **Proxy** → **Hermes Gateway** (port 30000+)
3. **Gateway** executes tool loop (full history maintained internally)
4. **Gateway** → SSE stream → **Proxy**
5. **Proxy** transforms format → **Open WebUI**
6. **Open WebUI** stores to local database
7. User sends new message → history reconstructed with tool context

### Dual Handler Design

The proxy uses two separate handlers for different API formats:

| Handler | Path | Features |
|---------|------|----------|
| `completions_handler.py` | `/v1/chat/completions` | enhance-v2 SSE transform, history sanitization |
| `responses_handler.py` | `/v1/responses` | Tool-result injection, SSE passthrough/convert |

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
- **upstreams** — Flexible routing table (see below)

Environment variables override config.yaml (`TOOL_MODE`, `BIND_PORT`, `BIND_HOST`, `AUTO_SPLIT_THRESHOLD`).

### Upstream Routing (Configurable)

Routing is fully configurable via `config.yaml` — no code changes needed to add or remove profiles:

```yaml
upstreams:
  "30000": "http://127.0.0.1:30000"
  "30001": "http://127.0.0.1:30001"
  "30002": "http://127.0.0.1:30002"
  "30003": "http://127.0.0.1:30003"
```

Each key is a path prefix, each value is the upstream Hermes Gateway URL. Common examples:

- `30000` → General-purpose chat
- `30001` → Code specialist
- `30002` → Data & research
- `30003` → Trading & markets

To view your actual profiles: `hermes profiles list`. Simply add or remove entries in `config.yaml` to match your setup. If `upstreams` is omitted, the four defaults above are used automatically.

### Hermes Gateway Configuration

The proxy routes to Hermes Gateway instances. Each Gateway profile has its own `.env` file — location depends on how Hermes was installed:

```
/opt/hermes/profiles/<PROFILE_NAME>/.env     # System-wide install
~/.hermes/profiles/<PROFILE_NAME>/.env       # User-level install
```

For example, a profile named `chatting` would be at `/opt/hermes/profiles/chatting/.env` or `~/.hermes/profiles/chatting/.env`. Key settings:

```bash
# Enable the API server
API_SERVER_ENABLED=true

# Gateway port — must match the port in your upstreams config
API_SERVER_PORT=30000

# API key — used to authenticate requests
API_SERVER_KEY=sk_YOUR_CUSTOM_KEY
```

> **Important:** The `API_SERVER_PORT` value must correspond to a port entry in your `upstreams` config. For example, if `API_SERVER_PORT=30001`, your upstreams must include `"30001": "http://127.0.0.1:30001"`.

When Open WebUI connects, set the API Base URL to `http://127.0.0.1:9099/<PORT>/v1` and the API key to the Gateway's `API_SERVER_KEY`.

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
- 🧹 **History sanitization** — 19 natural language templates across 5 tool categories, deterministic randomness for vLLM KV cache
- 📡 **Dual handler** — Separate handlers for Chat Completions and Responses API
- 💉 **Tool-result injection** — Automatic injection for stateful Responses API (dual-path: native + proxy)
- 📋 **Complete tool info** — Tool name, arguments, and results (child element format)
- ⚙️ **Config-driven** — Centralized management, no code changes needed

---

## History Sanitization (Anti-Pollution)

**Problem:** The `<details>` tags injected by enhance-v2 are stored as plain text in Open WebUI's conversation history. On the next request, the model sees these HTML tags in its prompt and starts mimicking them — outputting `<details>`, `<summary>`, `<arguments>`, `<result>` as part of its response. This creates a **pollution feedback loop** that gets worse over time.

**Solution:** Before forwarding requests to the upstream Gateway, the proxy scans all assistant messages and replaces `<details>` blocks with natural language descriptions.

### How It Works

1. Intercept incoming `messages` array
2. For each `role: "assistant"` message, find `<details type="tool_calls">` blocks
3. Extract tool name, arguments, and result from the block
4. Replace the block with a natural language sentence
5. Forward the cleaned request to the Gateway

### Example

**Before sanitization (polluted):**
```
好的喵～讓我查一下喵～

<details type="tool_calls" done="true" name="web_search">
<summary>✅ 🌐 web_search</summary>
<arguments>{"tool_name": "web_search", "query": "BTC price today"}</arguments>
<result>{"data": [{"title": "Bitcoin Price", ...}]}</result>
</details>

BTC 現在的價格大約是...
```

**After sanitization (clean):**
```
好的喵～讓我查一下喵～

搜尋「BTC price today」後獲得的資訊：{"data": [{"title": "Bitcoin Price", ...}]}

BTC 現在的價格大約是...
```

### 19 Natural Language Templates

To prevent the model from learning a fixed pattern, sanitization uses **19 different sentence templates** across 5 tool categories. A deterministic random selector (`random.Random(seed + index)`) ensures the same input always produces the same output — critical for **vLLM KV cache hits**.

| Tool Type | Templates | Tools |
|-----------|-----------|-------|
| Search | 4 | `web_search`, `brave_web_search`, `search_files`, `session_search` |
| Trading | 4 | `mcp_trading_get_positions`, `mcp_trading_get_wallet_balance`, ... |
| File | 3 | `read_file`, `write_file`, `patch` |
| Code | 3 | `execute_code`, `terminal` |
| General | 5 | All other tools |

### Configuration

Controlled via `config.yaml`:

```yaml
enable_history_sanitization: true
sanitization_result_max_length: 2000
```

---

## Responses API Support

**Problem:** When Open WebUI uses the Responses API in stateful mode (`previous_response_id`), it only carries user messages in the `input` — tool results from the previous turn are lost. The model has no visibility into what tools were executed or what they returned.

**Solution:** The `responses_handler.py` fetches the previous response and injects tool results as text summaries into the current input.

### Dual-Path Protection

- **Path A (native):** `previous_response_id` is preserved so Hermes can retrieve the full `conversation_history` internally (primary mechanism).
- **Path B (proxy injection):** Tool results are converted to text summaries and prepended to the user message as a fallback.

### Injection Format

```xml
<tool_results_from_previous_turn>
[Previous turn] Tool called: web_search(query=BTC price today)
[Previous turn] Tool result: {"data": [{"title": "Bitcoin Price", ...}]}
</tool_results_from_previous_turn>

<User's new message here>
```

### SSE Modes

| Mode | Behavior |
|------|----------|
| `passthrough` (default) | Forward Responses SSE events directly |
| `convert` | Convert Responses SSE → Chat Completions SSE |

### Configuration

```yaml
responses_sse_mode: "passthrough"  # or "convert"
```

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
| Model outputs `<details>` tags | Pollution feedback loop | Check `enable_history_sanitization: true` |
| Model forgets tool results in Responses API | Stateful mode not injecting | Check `[responses] Injected` in filter logs |
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
