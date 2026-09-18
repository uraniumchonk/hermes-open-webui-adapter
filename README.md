# Hermes Tool Filter

SSE proxy between **Open WebUI** and **Hermes Gateway** — `/v1/chat/completions`
and `/v1/responses`.

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

### `flat` — still one assistant string (legacy, frozen)

The older `flat` template (single assistant string with `[START_PREV_ACTION]`
hints) lived on the now-deleted `flat-history` branch. `main` ships `structured`
only.

---

## `/v1/responses` — session continuity

Open WebUI's Responses mode drops tool items between turns, so the agent loses
tool memory. The filter fixes this with a lightweight session marker:

1. **First turn** — no marker in the request → forward as-is; the response
   gets a visible session tag appended to the first assistant message:
   ````
   ```
   <!--hermes-sid:<id>-->
   ```
   ````
2. **Next turns** — the filter reads that one marker (forward scan, early
   exit — the huge middle history is never read), rewrites the request to
   `[last user]` + an `X-Hermes-Session-Id` header, and the Gateway reloads
   the full transcript (tool calls included) from its session DB.

The marker never reaches the model (the filter drops the payload history and
the Gateway strips any stray marker). One marker per chat, on the first
assistant message only.

---

## Download

https://github.com/uraniumchonk/hermes-open-webui-adapter

```bash
git clone https://github.com/uraniumchonk/hermes-open-webui-adapter.git
# or: https://github.com/uraniumchonk/hermes-open-webui-adapter/archive/refs/heads/main.zip
```

---

## Project structure

```
main.py                      # entry point: proxy routing, streaming, health
completions_handler.py       # /v1/chat/completions — tool-card enhance + sanitize
responses_handler.py         # /v1/responses — session continuity + tool results
responses_session.py         # sid marker extract/inject (responses path only)
tool_history_format.py       # tool card → model-safe history (structured)
tool_history_structured.py   # structured sanitizer (native tool roles)
native_tool_context.py       # native tool-context injection
special_tags.py              # neutralize special tags in tool results
special_tags.json            # tag list (data)
extract_tags.py              # one-off tag generator (re-run after model/OWUI upgrade)
comp_mode.py                 # optional tool-result compression
patches/                     # optional personal Hermes patches (see below)
config.yaml                  # sample config
requirements.txt
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
