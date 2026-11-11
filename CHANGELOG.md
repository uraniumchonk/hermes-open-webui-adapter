# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions are `MAJOR.MINOR.PATCH`,
and tags are cut from `main`.

## [Unreleased]

## [1.1.0] - 2026-11-11

### Added

- **Mid-turn steering.** A message sent while an answer is still streaming is folded into the
  running turn at its next tool boundary (`POST /v1/runs/{id}/steer`) instead of being queued
  behind it, and the follow-up gets an immediate ack in the chat.
- **Stop is a hold window, not an instant kill.** Pressing Stop keeps the upstream socket open
  for `stop_grace_seconds` (default 20) so the conversation stays addressable: a message typed
  inside that window steers into the running turn, and only if nobody takes over before the
  window closes does the proxy send `POST /v1/runs/{id}/stop`.
- `run_control.py` — per-conversation live run registry, with ack payloads in both the chat
  completions and responses shapes.
- `stream_enhance.on_upstream_meta` hook, so a handler can capture the gateway's run id.

### Fixed

- Stop/steer now carry the caller's own `Authorization` header. Without it the gateway answered
  `401` and the control silently fell back to queueing: the symptom was "Stop does nothing",
  with no error surfaced anywhere.
- Redact base64 inside the Responses `function_call_output` item. A large image pushed the local
  session past its 1 MiB line limit (`LineTooLong`), the stream broke mid-item, and Open WebUI
  reported `Tool "vision_analyze" not found` followed by `No user message found in input`.
  Small images were fine, phone photos (~1.6 MB) were not, so it looked intermittent.
- The `responses_session_id` reference patch now passes `history_from_session`, so a compressing
  turn runs on the compressed transcript instead of the pre-compression one (which rewrote token
  counts with the old, larger size and re-triggered compression next turn).

### Changed

- `patches/` keeps only the three Gateway reference patches the filter actually depends on;
  personal patches and the port watchdog moved to a private repo.

## [1.0.0] - 2026-09-26

### Added

- Two endpoints, two context models: `/v1/chat/completions` keeps history on the client (edit and
  retry any message), `/v1/responses` keeps it in the Hermes session DB (seamless compression,
  no payload growth).
- Slim-down architecture: `main.py` split into `runtime` and `stream_enhance`, dead code and
  disabled features removed, the two catch-all proxies merged into one.
- `/health` registered before the catch-all proxy route.

## [0.0.1] - 2026-09-18

### Added

- Initial release: SSE proxy between Open WebUI and the Hermes Gateway, Responses session marker
  for continuity, structured tool history so tool cards keep their arguments and results.
