# Reference patches for Hermes Gateway

The proxy runs on a stock Hermes, but four features need the Gateway to emit
more than upstream does. These patches are **reference snapshots** of how one
deployment does it — taken against Hermes v0.22.5 (`bf0203a794`). Hermes moves
fast, so expect line drift; read them as a description of the required
behaviour, not as guaranteed clean applies.

| Patch | Gateway behaviour this proxy relies on |
|---|---|
| `api_server_chat_completions_all.patch` | `hermes.tool.progress` SSE carries `arguments` + `result` (tool cards), with image base64 redacted out of progress events; Chat Completions history keeps `role=tool` / `tool_calls` and uses the last **user** message as the turn input |
| `api_server_responses_session_id.patch` | `/v1/responses` honours `X-Hermes-Session-Id`: the full transcript (tool calls included) is reloaded from SessionDB, DB beats body, and `<!--hermes-sid:…-->` markers are stripped before the model sees them; when that history is the untouched transcript it is passed with `history_from_session=True`, so the turn re-reads it after gateway hygiene (a compressing turn otherwise still sends the pre-compression history) |
| `api_server_run_control.patch` | streaming turns on **both** endpoints register as addressable runs: `GET /v1/runs` lists them, `POST /v1/runs/{id}/steer` folds a follow-up into the running turn, `POST /v1/runs/{id}/stop` ends it — including when the stop lands between two tool calls, where the interrupt flag would otherwise sit unread and the turn keeps issuing requests |
| `prompt_builder_api_server_hint.patch` | the api_server platform hint allows Markdown (needed for tool cards and formatting in OWUI) |

Without `api_server_run_control.patch` the proxy still works, but mid-turn steering and the Stop
hold window degrade to plain queueing: every call to `/v1/runs…` answers 404 and the proxy falls
back to forwarding the follow-up as a second request, which the gateway runs only after the first
turn finishes.

Apply order matters: three of the patches touch
`gateway/platforms/api_server_openai_routes.py` (+ `api_server_response_admissions.py`
for the session one, + `api_server_runs.py` / `turn_api_call.py` for run control),
so apply them in this order:

```bash
cd ~/.hermes/hermes-agent
git apply /path/to/patches/api_server_chat_completions_all.patch
git apply /path/to/patches/api_server_responses_session_id.patch
git apply /path/to/patches/api_server_run_control.patch
git apply /path/to/patches/prompt_builder_api_server_hint.patch
# restart the gateway profile(s), then check:
grep -c '_redact_progress_result' gateway/platforms/api_server_openai_routes.py           # >0
grep -c 'provided_session_id' gateway/platforms/api_server_response_admissions.py         # >0
grep -c '_run_owners' gateway/platforms/api_server_openai_routes.py                       # >0
grep -c 'interrupted before the next API call' agent/turn_api_call.py                     # >0
grep -c 'Markdown is allowed' agent/prompt_builder.py                                     # >0
```

`hermes update` stashes local changes and pops them back; when the pop
conflicts, re-apply by hand and keep upstream's structure.
