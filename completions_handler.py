"""
Completions Handler — 處理 /v1/chat/completions 端點。

從 main.py 遷移過來的現有邏輯，負責：
1. 接收 Open WebUI 的 Chat Completions 請求
2. 執行 history sanitization（清理 <details> 污染）
3. 轉發給 Hermes Gateway
4. 即時轉換 SSE stream（enhance-v2 模式）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import aiohttp
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse, Response

logger = logging.getLogger(__name__)


async def handle_completions_request(
    request: Request,
    upstream_url: str,
    fwd_headers: Dict[str, str],
    body: bytes,
    req_json: Dict[str, Any],
    sess: aiohttp.ClientSession,
    upstream_port: str,
    # 從 main.py 傳入的函數引用
    sanitize_request_messages,
    transform_stream,
) -> Any:
    """
    主處理器：處理所有 /v1/chat/completions 請求。
    
    支援：
    - 串流模式（SSE + enhance-v2 轉換）
    - 非串流模式（直接透傳）
    """
    model = req_json.get("model", "hermes-agent")
    stream_flag = req_json.get("stream", True)
    original_path = request.scope.get("path", "")

    # ✅ History Sanitization: 在轉發前清理 messages 中的 <details> 標籤
    if "messages" in req_json and isinstance(req_json["messages"], list):
        req_json["messages"] = sanitize_request_messages(req_json["messages"])
        body = json.dumps(req_json, ensure_ascii=False).encode("utf-8")

    # Detect client type from User-Agent to decide if we strip <details> tags
    user_agent = request.headers.get("user-agent", "").lower()
    strip_details = "dart" in user_agent or "conduit" in user_agent

    completion_id = f"chatcmpl-{int(time.time()*1000)}"
    created_ts = int(time.time())

    # --- Streaming path (chat completions with stream=true) ---
    if stream_flag and "chat/completions" in original_path:

        async def generate():
            upstream_resp = None
            try:
                upstream_resp = await sess.post(
                    upstream_url, data=body, headers=fwd_headers
                )
                logger.info(
                    f"[port={upstream_port}] Proxied chat completions, "
                    f"upstream status={upstream_resp.status}, "
                    f"strip_details={strip_details} (UA: {user_agent[:50]})"
                )
                async for chunk in transform_stream(
                    upstream_resp.content, model, completion_id, created_ts,
                    upstream_port, strip_details,
                ):
                    yield chunk
            except asyncio.CancelledError:
                logger.info(f"[port={upstream_port}] Client disconnected, closing upstream gracefully")
                if upstream_resp is not None:
                    upstream_resp.close()
            except aiohttp.ServerDisconnectedError:
                logger.info(f"[port={upstream_port}] Upstream disconnected (expected after auto-split)")
            except aiohttp.ClientError as e:
                logger.warning(f"[port={upstream_port}] Client error: {e}")
            except Exception as e:
                logger.error(f"[port={upstream_port}] Proxy error: {type(e).__name__}: {e}", exc_info=True)
                yield b'data: {"error":{"message":"Internal proxy error","type":"proxy_error","code":"upstream_failure"}}\n\n'
            finally:
                if upstream_resp is not None and not upstream_resp.closed:
                    upstream_resp.close()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # --- Non-streaming path (passthrough) ---
    method = request.method.upper()
    return await _passthrough_non_streaming(
        sess, method, upstream_url, body, fwd_headers
    )


async def _passthrough_non_streaming(
    sess: aiohttp.ClientSession,
    method: str,
    upstream_url: str,
    body: bytes,
    fwd_headers: Dict[str, str],
) -> Any:
    """非串流模式：直接透傳"""
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
