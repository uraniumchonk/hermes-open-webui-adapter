"""
Responses Handler — 處理 /v1/responses 端點。

Open WebUI 在 Responses API 模式下會直接發送 Responses 格式的請求：
- input: 字串或物件陣列
- instructions: system prompt
- previous_response_id: 用於維護對話狀態
- tools: Responses 格式的工具定義

Hermes Gateway 已經原生支援 Responses API，所以我們主要負責：
1. 路由轉發（POST / GET / DELETE）
2. 串流時將 Responses SSE 轉為 Chat Completions SSE（讓 Open WebUI 能正確解析）
3. 非串流時將 Responses JSON 轉為 Chat Completions JSON
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncGenerator, Dict, Optional

import aiohttp
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)


# ── Format Conversion: Responses ↔ Chat Completions ───────

def responses_to_chat_json(response: Dict[str, Any]) -> Dict[str, Any]:
    """
    將 Responses API 的回應轉為 Chat Completions JSON 格式。
    
    Responses: {id, output: [{type: message, content: [...]}], usage}
    Chat: {choices: [{message: {role, content, tool_calls}, finish_reason}], usage}
    """
    output_items = response.get("output", [])
    usage = response.get("usage", {})

    text_parts: list[str] = []
    tool_calls: list[dict] = []
    finish_reason = "stop"

    for item in output_items:
        item_type = item.get("type", "")
        
        if item_type == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text_parts.append(part.get("text", ""))
            if item.get("status") == "incomplete":
                finish_reason = "incomplete"
        
        elif item_type == "function_call":
            tool_calls.append({
                "id": item.get("id", item.get("call_id", "")),
                "type": "function",
                "function": {
                    "name": item.get("name", "unknown"),
                    "arguments": item.get("arguments", "{}"),
                },
            })
    
    if tool_calls:
        finish_reason = "tool_calls"

    return {
        "id": response.get("id", ""),
        "object": "chat.completion",
        "created": response.get("created_at", int(time.time())),
        "model": response.get("model", ""),
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "".join(text_parts) if text_parts else None,
                **({"tool_calls": tool_calls} if tool_calls else {}),
            },
            "finish_reason": finish_reason,
        }],
        "usage": usage,
    }


def responses_sse_to_chat_sse(
    event_type: str,
    data: Dict[str, Any],
    model: str,
    completion_id: str,
    created: int,
) -> Optional[bytes]:
    """
    將 Responses API 的 SSE 事件轉為 Chat Completions SSE 格式。
    
    支援的事件：
    - response.output_text.delta → delta.content
    - response.output_item.added (function_call) → delta.tool_calls
    - response.completed → finish chunk with usage
    """
    if event_type == "response.output_text.delta":
        delta_text = data.get("delta", "")
        if delta_text:
            payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": delta_text},
                    "finish_reason": None,
                }],
            }
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    elif event_type == "response.output_item.added":
        item = data.get("item", {})
        if item.get("type") == "function_call":
            payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {
                        "tool_calls": [{
                            "index": data.get("output_index", 0),
                            "id": item.get("id", item.get("call_id", "")),
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", ""),
                            },
                        }],
                    },
                    "finish_reason": None,
                }],
            }
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    elif event_type == "response.completed":
        resp_data = data.get("response", {})
        usage = resp_data.get("usage", {})
        output_items = resp_data.get("output", [])
        
        has_tool_calls = any(i.get("type") == "function_call" for i in output_items)
        finish = "tool_calls" if has_tool_calls else "stop"

        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": finish,
            }],
            "usage": usage,
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    return None


# ── Main Handler ───────────────────────────────────────────

async def handle_responses_request(
    request: Request,
    upstream_url: str,
    fwd_headers: Dict[str, str],
    body: bytes,
    req_json: Dict[str, Any],
    sess: aiohttp.ClientSession,
    config: dict,
) -> Any:
    """
    主處理器：處理所有 /v1/responses 請求。
    
    支援：
    - POST /v1/responses (建立對話)
    - GET /v1/responses/{id} (查詢)
    - DELETE /v1/responses/{id} (刪除)
    """
    method = request.method.upper()

    if method == "POST":
        return await _handle_post_responses(
            request, upstream_url, fwd_headers, body, req_json, sess, config
        )
    elif method == "GET":
        return await _handle_get_response(upstream_url, fwd_headers, sess)
    elif method == "DELETE":
        return await _handle_delete_response(upstream_url, fwd_headers, sess)
    else:
        return JSONResponse(
            content={"error": {"message": f"Method {method} not supported for /v1/responses", "type": "not_supported"}},
            status_code=405,
        )


async def _handle_post_responses(
    request: Request,
    upstream_url: str,
    fwd_headers: Dict[str, str],
    body: bytes,
    req_json: Dict[str, Any],
    sess: aiohttp.ClientSession,
    config: dict,
) -> Any:
    """處理 POST /v1/responses 請求"""
    model = req_json.get("model", "hermes-agent")
    stream_flag = req_json.get("stream", False)
    
    # 取得設定
    sse_mode = config.get("responses_sse_mode", "passthrough")

    if stream_flag:
        return await _stream_responses(
            upstream_url, fwd_headers, body, model, sse_mode
        )
    else:
        return await _blocking_responses(
            upstream_url, fwd_headers, body
        )


async def _blocking_responses(
    upstream_url: str,
    fwd_headers: Dict[str, str],
    body: bytes,
) -> JSONResponse:
    """
    非串流模式：等待完整回應後再回傳。
    
    直接透傳 Responses API 的 JSON 回應，因為 Open WebUI 在 Responses 模式下
    期望的就是 Responses 格式的回應。
    """
    timeout = aiohttp.ClientTimeout(total=600, connect=10, sock_read=600)
    async with aiohttp.ClientSession(timeout=timeout, max_line_size=65536) as local_sess:
        async with local_sess.post(
            upstream_url, data=body, headers=fwd_headers
        ) as resp:
            resp_body = await resp.json()
            logger.info(
                f"[responses] Non-streaming response received, "
                f"status={resp.status}, output_items={len(resp_body.get('output', []))}"
            )
            return JSONResponse(content=resp_body, status_code=resp.status)


async def _stream_responses(
    upstream_url: str,
    fwd_headers: Dict[str, str],
    body: bytes,
    model: str,
    sse_mode: str,
) -> StreamingResponse:
    """
    串流模式：SSE passthrough 或轉換。
    
    當 sse_mode="passthrough" 時，直接透傳 Responses SSE 事件。
    當 sse_mode="convert" 時，將 Responses SSE 轉為 Chat Completions SSE。
    """
    completion_id = f"chatcmpl-{int(time.time()*1000)}"
    created_ts = int(time.time())

    async def generate() -> AsyncGenerator[bytes, None]:
        timeout = aiohttp.ClientTimeout(total=600, connect=10, sock_read=600)
        async with aiohttp.ClientSession(timeout=timeout, max_line_size=65536) as local_sess:
            try:
                async with local_sess.post(
                    upstream_url, data=body, headers=fwd_headers
                ) as resp:
                    logger.info(
                        f"[responses] Streaming started, mode={sse_mode}, "
                        f"upstream_status={resp.status}"
                    )

                    if sse_mode == "passthrough":
                        # 直接透傳 SSE 事件（Open WebUI 能處理原生 Responses SSE）
                        async for chunk in resp.content:
                            yield chunk
                    else:
                        # 轉換模式：將 Responses SSE 轉為 Chat Completions SSE
                        async for chunk in _stream_and_convert(
                            resp, model, completion_id, created_ts
                        ):
                            yield chunk
                        return

            except Exception as e:
                logger.error(f"[responses] Streaming error: {type(e).__name__}: {e}")
                yield b'data: {"error":{"message":"Internal proxy error","type":"proxy_error"}}\n\n'

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_and_convert(
    resp: aiohttp.ClientResponse,
    model: str,
    completion_id: str,
    created_ts: int,
) -> AsyncGenerator[bytes, None]:
    """讀取 Responses SSE 並轉換為 Chat Completions SSE"""
    buffer = b""
    last_heartbeat = time.monotonic()

    while True:
        # 心跳
        elapsed = time.monotonic() - last_heartbeat
        if elapsed >= 15:
            yield b": keepalive\n\n"
            last_heartbeat = time.monotonic()

        try:
            line = await asyncio.wait_for(resp.content.readline(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        if not line:
            break

        buffer += line

        while b"\n\n" in buffer:
            frame_bytes, buffer = buffer.split(b"\n\n", 1)
            frame = frame_bytes.decode("utf-8", errors="replace")

            lines = frame.strip().split("\n")
            event_type = None
            data_lines = []

            for line_item in lines:
                if line_item.startswith("event: "):
                    event_type = line_item[7:].strip()
                elif line_item.startswith("data:"):
                    data_lines.append(line_item[5:].lstrip(" "))

            if not event_type or not data_lines:
                continue

            data_str = "\n".join(data_lines)
            try:
                data_obj = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            chunk = responses_sse_to_chat_sse(
                event_type, data_obj, model, completion_id, created_ts
            )
            if chunk:
                yield chunk

            last_heartbeat = time.monotonic()


async def _handle_get_response(
    upstream_url: str,
    fwd_headers: Dict[str, str],
    sess: aiohttp.ClientSession,
) -> JSONResponse:
    """處理 GET /v1/responses/{id}"""
    async with sess.get(upstream_url, headers=fwd_headers) as resp:
        resp_body = await resp.json()
        return JSONResponse(content=resp_body, status_code=resp.status)


async def _handle_delete_response(
    upstream_url: str,
    fwd_headers: Dict[str, str],
    sess: aiohttp.ClientSession,
) -> JSONResponse:
    """處理 DELETE /v1/responses/{id}"""
    async with sess.delete(upstream_url, headers=fwd_headers) as resp:
        resp_body = await resp.json()
        return JSONResponse(content=resp_body, status_code=resp.status)
