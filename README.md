# Hermes Tool Filter

SSE proxy between **Open WebUI** and **Hermes Gateway** (`/v1/chat/completions` only).

English · [繁體中文](README.zh-TW.md)

---

## Problem

Hermes runs tools correctly inside the agent loop, but Open WebUI only persists
HTML tool cards inside assistant text. On the **next** request that HTML is sent
back as normal assistant content → the model forgets tools or starts mimicking
`<details>`.

This proxy:

1. **Outbound** — turns Hermes `hermes.tool.progress` into Open WebUI tool cards  
2. **Inbound** — rewrites those cards into model-safe chat history before Gateway

```
Open WebUI → hermes_tool_filter :9099 → Hermes Gateway :3000x → model
```

Open WebUI base URL: `http://127.0.0.1:9099/<port>/v1`

---

## What the next-turn payload looks like

Open WebUI stores something like this (simplified):

```json
{
  "role": "assistant",
  "content": "Let me check.\n\n<details type=\"tool_calls\" done=\"true\" name=\"web_search\">\n<summary>web_search</summary>\n<arguments>{\"query\": \"BTC price\"}</arguments>\n<result>{\"price\": 64000}</result>\n</details>\n\nAbout 64000."
}
```

Without the filter, the model sees that whole string again.  
With the filter, history is rewritten. **Two templates:**

### `structured` — native tool roles (current `main`)

```json
[
  {
    "role": "assistant",
    "content": "Let me check.",
    "tool_calls": [{
      "id": "call_htf_a1b2",
      "type": "function",
      "function": {
        "name": "web_search",
        "arguments": "{\"query\": \"BTC price\"}"
      }
    }]
  },
  {
    "role": "tool",
    "tool_call_id": "call_htf_a1b2",
    "name": "web_search",
    "content": "{\"price\": 64000}"
  },
  {
    "role": "assistant",
    "content": "About 64000."
  }
]
```

### `flat` — still one assistant string (legacy branch)

```json
{
  "role": "assistant",
  "content": "Let me check.\n\n[START_PREV_ACTION]\n[ACTION_TYPE]\nweb_search\n[ACTION_ARG]\nquery: BTC price\n[RESULT]\nprice: 64000\n[END_PREV_ACTION]\n\nAbout 64000."
}
```

| | `structured` | `flat` |
|--|--------------|--------|
| Roles the model sees | `assistant` + `tool` | `assistant` only |
| Pollution | Structural | Hint-based |
| Branch | **`main`** (only option) | **`flat-history`** (frozen) |

---

## Download

https://github.com/uraniumchonk/hermes-open-webui-adapter

| Want | Branch | Link |
|------|--------|------|
| Current (structured only) | `main` | [ZIP](https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/heads/main.zip) |
| Need `flat` switch | `flat-history` | [ZIP](https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/heads/flat-history.zip) · [tree](https://github.com/uraniumchonk/hermes-open-webui-adapter/tree/flat-history) |

```bash
git clone -b main https://github.com/uraniumchonk/hermes-open-webui-adapter.git
# or: git clone -b flat-history ...
```

---

## Setup

```bash
pip install -r requirements.txt
# edit upstreams in config.yaml
python main.py
```

```yaml
upstreams:
  "30001": "http://127.0.0.1:30001"

tool_mode: "enhance-v2"
enable_history_sanitization: true
sanitization_result_max_length: 20000
```

Gateway `.env`: `API_SERVER_ENABLED=true`, `API_SERVER_PORT` matches the upstream key, `API_SERVER_KEY=...`.

### Optional: local Hermes patches

The proxy works without modifying Hermes.  
`patches/` holds **optional personal patches** used on some setups for a better experience
(e.g. richer tool-progress payloads, allowing Markdown on api_server, keeping
`role=tool` through Chat Completions). They are **not required** to run this project.

If you use them, re-apply after `hermes update`. Line numbers drift — treat the
files as references, not guaranteed clean applies on every Hermes version.

---

## License

MIT
