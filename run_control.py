"""Mid-turn control for Open WebUI conversations: steer, don't queue.

The gateway admits **one turn per session**. Open WebUI, while an answer is still
streaming, opens a *second* ``/v1/chat/completions`` (or ``/v1/responses``) request for
the same conversation — the user's correction. The gateway queues that behind the running
turn, so the correction is not even read until the whole answer finishes (measured: a
one-word follow-up waited 72.8 s behind a ``sleep 80`` turn).

This module keeps the live run id per conversation, read off the upstream SSE stream, and
turns such a follow-up into ``POST /v1/runs/{id}/steer`` — the agent folds the correction
into the turn it is already doing. The same handle covers Stop: if the client drops, the
proxy asks the gateway to stop that exact run instead of trusting the socket teardown
alone.

Run ids only exist because of the ``api_server_run_control`` patch (streaming entries
register themselves as runs). Without it every call here answers 404 and we fall back to
plain proxying.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)

# conversation key -> {"run_id": str, "upstream": str, "ts": float, "kind": str}
ACTIVE: Dict[str, Dict[str, Any]] = {}
STALE_AFTER_SECONDS = 900  # a turn that has not touched its record in 15 min is gone
_STEER_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)


def conversation_key(req_json: Dict[str, Any]) -> Optional[str]:
    """Conversation identity for an Open-WebUI style request without a session header.

    Mirrors the gateway's own derivation (``_derive_chat_session_id``:
    ``sha256(f"{system}\\n{first_user}")[:16]`` -> ``api-<hex>``), so a follow-up request
    lands on the same key as the turn it means to correct. Returns None when the body has
    no user text at all (nothing to steer, nothing to key on).
    """
    system = ""
    first_user = ""

    def _text_of(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") in (None, "text", "input_text")
            )
        return ""

    messages = req_json.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "system" and not system:
                system = _text_of(msg.get("content"))
            elif role == "user" and not first_user:
                first_user = _text_of(msg.get("content"))

    if not first_user:
        raw_input = req_json.get("input")
        if isinstance(raw_input, str):
            first_user = raw_input
        elif isinstance(raw_input, list):
            for item in raw_input:
                if isinstance(item, dict) and item.get("role") == "user":
                    first_user = _text_of(item.get("content"))
                    break

    if not first_user.strip():
        return None
    seed = f"{system}\n{first_user}"
    return "api-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def last_user_text(req_json: Dict[str, Any]) -> str:
    """The newest user message in the request — the correction to steer with."""
    def _text_of(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") in (None, "text", "input_text")
            ).strip()
        return ""

    messages = req_json.get("messages")
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                text = _text_of(msg.get("content"))
                if text:
                    return text
    raw_input = req_json.get("input")
    if isinstance(raw_input, str):
        return raw_input.strip()
    if isinstance(raw_input, list):
        for item in reversed(raw_input):
            if isinstance(item, dict) and item.get("role") == "user":
                text = _text_of(item.get("content"))
                if text:
                    return text
    return ""


def note_active(key: Optional[str], run_id: Optional[str], upstream: str, kind: str,
                auth_header: Optional[str] = None) -> None:
    """Remember the live run for this conversation (called as soon as the id is known).

    ``auth_header`` is the caller's own Authorization value: the run-control routes are
    authenticated per API key, so a stop/steer sent without it comes back 401 and the
    follow-up silently falls back to queueing.
    """
    if not key or not run_id:
        return
    ACTIVE[key] = {"run_id": run_id, "upstream": upstream.rstrip("/"), "ts": time.time(),
                   "kind": kind, "auth": auth_header or ""}
    logger.info(f"[run-control] active {kind} run {run_id} for {key[:16]}…")


def clear_active(key: Optional[str], run_id: Optional[str] = None) -> None:
    """Forget the run once it settles — but never a *newer* run recorded under the same key."""
    if not key:
        return
    record = ACTIVE.get(key)
    if record is None:
        return
    if run_id is not None and record.get("run_id") != run_id:
        return
    ACTIVE.pop(key, None)


def active_run(key: Optional[str]) -> Optional[Dict[str, Any]]:
    if not key:
        return None
    record = ACTIVE.get(key)
    if record is None:
        return None
    if time.time() - float(record.get("ts") or 0) > STALE_AFTER_SECONDS:
        ACTIVE.pop(key, None)
        return None
    return record


async def _post_run_control(sess: Optional[aiohttp.ClientSession], upstream: str, run_id: str,
                            action: str, payload: Optional[dict] = None,
                            auth_header: Optional[str] = None) -> Optional[dict]:
    url = f"{upstream.rstrip('/')}/v1/runs/{run_id}/{action}"
    headers = {"Content-Type": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header

    async def _do(client: aiohttp.ClientSession) -> Optional[dict]:
        async with client.post(url, json=payload or {}, headers=headers, timeout=_STEER_TIMEOUT) as resp:
            body = await resp.text()
            if resp.status >= 400:
                logger.info(f"[run-control] {action} {run_id} refused: {resp.status} {body[:160]}")
                return None
            try:
                return json.loads(body) if body else {}
            except json.JSONDecodeError:
                return {}

    try:
        if sess is None:
            # Called from a cancelled stream generator: the caller's session may already be
            # closing, so spend a fresh short-lived one to get the stop out.
            async with aiohttp.ClientSession(timeout=_STEER_TIMEOUT) as own:
                return await _do(own)
        return await _do(sess)
    except Exception as exc:  # network/timeout: the caller falls back to plain proxying
        logger.info(f"[run-control] {action} {run_id} failed: {type(exc).__name__}: {exc}")
        return None


async def stop_active(key: Optional[str], sess: Optional[aiohttp.ClientSession] = None) -> bool:
    """Ask the gateway to stop the run this conversation still owns (client disconnect).

    The socket teardown already interrupts the agent on a normally-behaving client; this
    makes it explicit and survives proxy layers that keep the upstream request open after
    the browser gave up.
    """
    record = active_run(key)
    if record is None:
        return False
    if await _post_run_control(sess, record["upstream"], record["run_id"], "stop",
                               auth_header=record.get("auth")) is None:
        return False
    logger.info(f"[run-control] stop sent for {record['run_id']}")
    clear_active(key, record["run_id"])
    return True


async def steer_active(key: Optional[str], text: str, sess: Optional[aiohttp.ClientSession] = None) -> bool:
    record = active_run(key)
    if record is None or not text:
        return False
    result = await _post_run_control(sess, record["upstream"], record["run_id"], "steer",
                                     {"input": text}, auth_header=record.get("auth"))
    if result is None:
        return False
    # The user's own follow-up took over this turn: do not hard-stop it out from under them.
    record["stop_pending"] = False
    logger.info(f"[run-control] steered {record['run_id']} with {len(text)} chars")
    return True


# ── Stop with a grace window ────────────────────────────────────────────────
#
# A hard stop mid-tool is the wrong shape for this UI: the run flips to cancelled, but the
# agent is between tool calls, so nothing aborts and it keeps issuing requests until the turn
# ends on its own (measured: 56 s / 11 API calls after the user pressed Stop, and the follow-up
# message sat queued behind it).
#
# What the user asked for instead: pressing Stop should hold for a few seconds, let the turn
# reach its next tool boundary, fold "stop now" in as steering, and only kill it if it is still
# running after the window. That is exactly what grace_stop does.

_DEFAULT_STOP_STEER = (
    "使用者按下停止。請立刻停止目前的工作，用一兩句話回報已完成到哪裡，"
    "不要再開新的工具呼叫。"
)


def stop_grace_seconds() -> float:
    try:
        from runtime import CONFIG
        return float(CONFIG.get("stop_grace_seconds", 20))
    except Exception:
        return 20.0


def stop_grace_steer_text() -> str:
    try:
        from runtime import CONFIG
        return str(CONFIG.get("stop_grace_steer") or _DEFAULT_STOP_STEER)
    except Exception:
        return _DEFAULT_STOP_STEER


async def grace_stop(key: Optional[str], upstream_resp, sess: Optional[aiohttp.ClientSession] = None,
                     close_after=None) -> None:
    """Hold the turn open after Stop so the user's own follow-up can still reach it.

    Pressing Stop in this UI used to mean "hard interrupt now", which is the worst of both
    worlds: the run was marked cancelled, but the agent was between tool calls so nothing
    aborted, and it kept issuing requests (measured: 56 s / 11 API calls) while the follow-up
    message sat queued behind it.

    What we do instead: keep the upstream socket open — the gateway never sees a dead client,
    so it does not hard-interrupt on its own — and keep draining it. The conversation stays
    addressable for ``stop_grace_seconds``: if the user types again, ``_maybe_steer`` folds that
    text into the running turn at its next tool boundary and clears ``stop_pending``. Only if
    nobody takes over before the window closes do we POST /stop.

    ``close_after`` is an owned session the handler could not close itself (it lost the
    ``async with`` frame on cancel); it is closed when this watcher is done.
    """
    record = active_run(key)
    if record is None:
        _close_quietly(upstream_resp)
        await _close_session(close_after)
        return
    run_id = record["run_id"]
    grace = stop_grace_seconds()
    record["stop_pending"] = True
    logger.info(f"[run-control] stop held for {run_id}: {grace:.0f}s window for a follow-up message")

    deadline = time.monotonic() + grace
    content = getattr(upstream_resp, "content", None)
    while content is not None:
        try:
            await asyncio.wait_for(content.readany(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        except Exception:
            break
        if content.at_eof():
            logger.info(f"[run-control] {run_id} finished on its own during the hold")
            break
        if record.get("stop_pending") and time.monotonic() >= deadline:
            logger.info(f"[run-control] {run_id} nobody took over in {grace:.0f}s — hard stop")
            record["stop_pending"] = False
            await stop_active(key, sess)
            # Fall through: keep draining until EOF so the gateway's SSE writes never block.
    _close_quietly(upstream_resp)
    await _close_session(close_after)


async def _close_session(session) -> None:
    if session is None:
        return
    try:
        await session.close()
    except Exception:
        pass


def _close_quietly(upstream_resp) -> None:
    try:
        if upstream_resp is not None and not getattr(upstream_resp, "closed", True):
            upstream_resp.close()
    except Exception:
        pass


def steer_ack_sse(completion_id: str, model: str) -> bytes:
    """Synthetic SSE for a follow-up that became a steer.

    Open WebUI expects a stream for this message; it shows this short note while the real
    work continues in the other turn.
    """
    created = int(time.time())
    note = "（已插入當前回應中，繼續執行…）"
    frames = [
        {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
         "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
         "choices": [{"index": 0, "delta": {"content": note}, "finish_reason": None}]},
        {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    out = b"".join(f"data: {json.dumps(f, ensure_ascii=False)}\n\n".encode("utf-8") for f in frames)
    return out + b"data: [DONE]\n\n"


def steer_ack_json(completion_id: str, model: str) -> dict:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "（已插入當前回應中，繼續執行…）"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


_STEER_NOTE = "（已插入當前回應中，繼續執行…）"


def steer_ack_responses_sse(response_id: str, model: str) -> bytes:
    """Responses-shaped SSE for a follow-up that became a steer (Open WebUI uses /v1/responses)."""
    now = int(time.time())
    item_id = f"msg_steer_{now}"
    item = {"id": item_id, "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": _STEER_NOTE}]}
    events = [
        ("response.created", {"type": "response.created", "response": {
            "id": response_id, "object": "response", "created_at": now,
            "status": "in_progress", "model": model, "output": []}}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "item_id": item_id,
                                        "output_index": 0, "content_index": 0, "delta": _STEER_NOTE}),
        ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0,
                                       "item": item}),
        ("response.completed", {"type": "response.completed", "response": {
            "id": response_id, "object": "response", "created_at": now,
            "status": "completed", "model": model, "output": [item]}}),
    ]
    return "".join(
        f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        for name, payload in events
    ).encode("utf-8")


def steer_ack_responses_json(response_id: str, model: str) -> dict:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": [{
            "id": f"msg_steer_{int(time.time())}",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": _STEER_NOTE}],
        }],
    }
