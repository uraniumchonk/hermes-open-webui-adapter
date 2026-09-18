# Hermes Tool Filter

SSE proxy between **Open WebUI** and **Hermes Gateway** — two endpoints, two
context models. Pick the one that fits how you chat.

English · [繁體中文](README.zh-TW.md)

**v0.0.1**

---

## Two endpoints, pick your context model

| | `/v1/chat/completions` | `/v1/responses` |
|---|---|---|
| Context lives on | **Client** (Open WebUI) | **Server** (Hermes session DB) |
| Each turn sends | Full history | Just the latest message |
| Edit / retry old messages | ✅ Yes | ❌ No (server-side session) |
| Hermes context compression | ❌ No | ✅ Yes — seamless, like TUI |
| Long continuous sessions | Payload grows | ✅ Continuous, no bloat |
| Best for | Daily chat, tweak & retry | Long agent sessions, tool-heavy work |

### `/v1/chat/completions` — client-side context

- Open WebUI stores the full history and sends it back every turn.
- You can **edit and retry** any message — great for everyday back-and-forth.
- **No server-side compression**: the history OWUI sends is what the model sees,
  so long chats grow the payload.
- **Best for:** short-to-medium daily conversations where you want to tweak
  and retry.

### `/v1/responses` — server-side context

- You send just the latest message; the Gateway reloads the full transcript
  (tool calls included) from its session DB.
- **Seamless Hermes context compression** — the session persists server-side,
  so Hermes compresses exactly like TUI/CLI mode.
- The conversation runs **continuously** like TUI mode — no payload bloat, no
  client-side history to manage.
- **Cannot edit or retry old messages** — the session is server-side, so
  rewriting the past isn't meaningful; you continue forward.
- **Best for:** long, continuous, tool-heavy agent sessions.

---

## How it works

Both endpoints fix the same core problem: Hermes runs tools correctly inside
the agent loop, but Open WebUI only persists HTML tool cards inside assistant
text. On the **next** request that HTML is sent back as normal assistant
content → the model forgets tools or starts mimicking `<details>`.

The proxy:

1. **Outbound** — turns Hermes `hermes.tool.progress` into Open WebUI tool cards
2. **Inbound** — rewrites those cards into model-safe chat history (native
   `assistant` + `tool` roles) before the Gateway

```
Open WebUI → hermes_tool_filter :9099 → Hermes Gateway :3000x → model
```

Open WebUI base URL: `http://127.0.0.1:9099/<port>/v1`

### Session continuity (responses only)

Open WebUI's Responses mode drops tool items between turns, so the agent loses
tool memory. The filter fixes this with a lightweight session marker:

1. **First turn** — no marker in the request → forward as-is; the response
   gets a visible session tag appended to the first assistant message:
   ````
   ```
   <!--hermes-sid:<id>-->
   ```
   ````
2. **Next turns** — the filter reads that one marker (forward scan, early exit —
   the huge middle history is never read), rewrites the request to
   `[last user]` + an `X-Hermes-Session-Id` header, and the Gateway reloads the
   full transcript (tool calls included) from its session DB.

The marker never reaches the model (the filter drops the payload history and
the Gateway strips any stray marker). One marker per chat, on the first
assistant message only.

---

## Setup

```bash
git clone https://github.com/uraniumchonk/hermes-open-webui-adapter.git
cd hermes-open-webui-adapter
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

Gateway `.env`: `API_SERVER_ENABLED=true`, `API_SERVER_PORT` matches the
upstream key, `API_SERVER_KEY=...`.

### Optional: local Hermes patches

The proxy works without modifying Hermes.
`patches/` holds **optional personal patches** used on some setups for a better
experience (e.g. richer tool-progress payloads, allowing Markdown on
api_server, keeping `role=tool` through Chat Completions, the responses
session-id header). They are **not required** to run this project.

If you use them, re-apply after `hermes update`. Line numbers drift — treat the
files as references, not guaranteed clean applies on every Hermes version.

---

## For AI agents

This repo ships an **`AGENTS.md`** — a complete operating guide: architecture,
file map, the 8 Hermes patches (apply order + grep verification + re-export),
two-machine deployment, and the standing maintenance your human needs to know.

Hand the repo straight to your agent (Hermes, Claude Code, Codex, …):

> Read `AGENTS.md` and set up this system on my machine.

It can install the deps, wire `config.yaml`, apply the patches, and deploy —
and it will brief your human on the ongoing maintenance. No manual reading
required.

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
patches/                     # optional personal Hermes patches (see above)
config.yaml                  # sample config
requirements.txt
```

---

## License

MIT
