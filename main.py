#!/usr/bin/env python3
"""
Hermes SSE Tool Card Enhancer Proxy (Multi-Tenant Router)

在 Open WebUI 和多個 Hermes Gateway profiles 之間的透明代理路由器。

路由規則（config.yaml upstreams）：
  /<port>/v1/*  → http://127.0.0.1:<port>/v1/*
  其他路徑       → DEFAULT_UPSTREAM（:30000）

模組：
  runtime.py              logging / config / crash debug / 記憶體保護 / 共用 aiohttp session
  completions_handler.py  /v1/chat/completions：history sanitize（入）+ 轉發
  stream_enhance.py       SSE 轉換：hermes.tool.progress → <details> tool card（出）
  responses_handler.py    /v1/responses：sid marker session 續接

配置：config.yaml（env BIND_PORT / BIND_HOST 可覆寫）
Systemd service: hermes-tool-filter.service
"""

import json
import time
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse

from runtime import (
    CONFIG, BIND_HOST, BIND_PORT, PORT_MAP, DEFAULT_UPSTREAM, MAX_REQUEST_BODY,
    logger, get_session, mem_guard_reject, start_health_dump,
)
from completions_handler import handle_completions_request
from responses_handler import handle_responses_request

APP = FastAPI(title="Hermes Tool Card Enhancer Router")


# ── Upstream Resolver ─────────────────────────────────────

def resolve_upstream(path: str) -> str:
    """
    根據路徑解析目標 upstream。

    /30000/v1/chat/completions  -> http://127.0.0.1:30000/v1/chat/completions
    /30001/v1/models           -> http://127.0.0.1:30001/v1/models
    /v1/models                 -> http://127.0.0.1:30000/v1/models  (default)
    """
    # Strip leading slash
    stripped = path.lstrip("/")

    # Try each port prefix
    for port, base in PORT_MAP.items():
        if stripped.startswith(port + "/"):
            remainder = stripped[len(port) + 1 :]
            return base + "/" + remainder
        # Also match just the port alone (e.g. /30001)
        if stripped == port:
            return base

    # Default: prepend to DEFAULT_UPSTREAM
    return DEFAULT_UPSTREAM + "/" + stripped


# ── Health Check ───────────────────────────────────────────

# Must be registered before the catch-all proxy route, or it is shadowed.
@APP.get("/health")
async def health():
    return {
        "status": "ok",
        "ports": {p: u for p, u in PORT_MAP.items()},
        "default_upstream": DEFAULT_UPSTREAM,
    }


# ── Route: Catch-all proxy ────────────────────────────────

_FORWARD_HEADERS = ("authorization", "content-type", "x-hermes-session-id", "x-hermes-session-key")


def _proxy_error(status: int, code: str, message: str, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(
        content={"error": {"message": message, "type": "proxy_error", "code": code}},
        status_code=status,
        headers=headers,
    )


@APP.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    """
    Single catch-all proxy. /<port>/v1/* goes to that port's Gateway,
    anything else to DEFAULT_UPSTREAM (see resolve_upstream).

    Routes:
    - */v1/responses/**      → responses_handler
    - */v1/chat/completions  → completions_handler (enhance-v2)
    - other paths            → passthrough
    """
    original_path = f"/{path}"
    upstream_port = path.split("/", 1)[0]
    upstream_url = resolve_upstream(original_path)
    req_id = path[:60]
    start_time = time.monotonic()
    logger.info(f"[req-trace] ENTER {request.method} {original_path[:80]} req_id={req_id}")

    # ── Memory self-protection: reject under pressure before reading body ──
    if mem_guard_reject():
        return _proxy_error(503, "memory_pressure", "Proxy under memory pressure, retry later",
                            headers={"Retry-After": "5"})

    # ── Request body cap: refuse pathological request bodies ──
    too_large = f"Request body exceeds {MAX_REQUEST_BODY // (1024*1024)}MB limit"
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_REQUEST_BODY:
        return _proxy_error(413, "body_too_large", too_large)
    body = await request.body()
    if len(body) > MAX_REQUEST_BODY:
        return _proxy_error(413, "body_too_large", too_large)
    body_read_ms = (time.monotonic() - start_time) * 1000

    # Forward auth/content-type + Hermes session headers only
    fwd_headers = {hn: hv for hn, hv in request.headers.items() if hn.lower() in _FORWARD_HEADERS}

    try:
        req_json = json.loads(body) if body else {}
    except json.JSONDecodeError:
        req_json = {}

    msg_count = msg_chars = 0
    if "messages" in req_json and isinstance(req_json["messages"], list):
        msg_count = len(req_json["messages"])
        msg_chars = sum(len(str(m.get("content", ""))) for m in req_json["messages"])

    sess = await get_session()

    if "/v1/responses" in original_path:
        route = "responses"
    elif "/v1/chat/completions" in original_path:
        route = "completions"
    else:
        route = "passthrough"
    logger.info(f"[req-trace] ROUTE to {route} req_id={req_id}")
    if route != "passthrough":
        logger.info(
            f"[perf] REQ body={len(body)}B msgs={msg_count} chars={msg_chars} "
            f"body_read={body_read_ms:.1f}ms req_id={req_id}"
        )

    if route == "responses":
        result = await handle_responses_request(
            request, upstream_url, fwd_headers, body, req_json, sess, CONFIG
        )
    elif route == "completions":
        result = await handle_completions_request(
            request, upstream_url, fwd_headers, body, req_json, sess, upstream_port,
        )
    else:
        # Passthrough for other endpoints (/v1/models, etc.)
        result = await _passthrough(request, upstream_url, fwd_headers, body, sess)

    logger.info(
        f"[perf] EXIT {route} req_id={req_id} TOTAL={time.monotonic() - start_time:.1f}s "
        f"body={len(body)}B msgs={msg_count}"
    )
    return result


async def _passthrough(request, upstream_url, fwd_headers, body, sess):
    """通用透傳：不處理，直接轉發"""
    method = request.method.upper()
    resp_body = b""
    resp_status = 502
    try:
        async with sess.request(
            method, upstream_url, data=body, headers=fwd_headers
        ) as resp:
            resp_body = await resp.read()
            resp_status = resp.status
            try:
                parsed = json.loads(resp_body) if resp_body else {}
            except json.JSONDecodeError:
                parsed = {}
            return JSONResponse(content=parsed, status_code=resp_status)
    except Exception:
        return Response(content=resp_body, status_code=resp_status)


# ── Start background health dump task ──
@APP.on_event("startup")
async def _on_startup():
    """Start background tasks when the server starts."""
    await start_health_dump()


# ── Main ───────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    logger.info("=" * 60)
    logger.info("Hermes Tool Card Enhancer Proxy (Multi-Tenant)")
    logger.info(f"Listening on http://{BIND_HOST}:{BIND_PORT}")
    logger.info("-" * 60)
    for port, url in PORT_MAP.items():
        logger.info(f"  /{port}/v1/*  ->  {url}/v1/*")
    logger.info(f"Default upstream: {DEFAULT_UPSTREAM}")
    logger.info("=" * 60)
    logger.info(f"Crash debug: SIGUSR2 handler registered (kill -USR2 <pid> for thread dump)")
    uvicorn.run(APP, host=BIND_HOST, port=BIND_PORT, log_level="info")
