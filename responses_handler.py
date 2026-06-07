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
    
    # ── Inject previous tool results into input ──
    # When Open WebUI sends a stateful request with previous_response_id,
    # it only carries user messages in the input — tool results from the
    # previous turn are lost.  Fetch the prior response and inject its
    # function_call / function_call_output items so the model can see them.
    modified_body = await _inject_previous_tool_results(
        request, upstream_url, fwd_headers, req_json, sess, config
    )
    if modified_body is not None:
        body = modified_body

    if stream_flag:
        return await _stream_responses(
            upstream_url, fwd_headers, body, model, sse_mode
        )
    else:
        return await _blocking_responses(
            upstream_url, fwd_headers, body
        )


async def _inject_previous_tool_results(
    request: Request,
    upstream_url: str,
    fwd_headers: Dict[str, str],
    req_json: Dict[str, Any],
    sess: aiohttp.ClientSession,
    config: dict,
) -> Optional[bytes]:
    """
    當請求帶有 previous_response_id 時，取得上一輪的 response 並將其
    tool results 以文字摘要形式注入到 user message 中。
    
    問題：
    Open WebUI 在 stateful 模式下只把 user messages 帶到 input 中，
    而 Hermes API Server 的 input parser / conversation_history parser
    都無法完整保留 tool_calls / tool_call_id 等非標準欄位。
    
    方案（雙路徑）：
    路徑 A — native conversation_history：保留 previous_response_id，
    Hermes 內部從 _response_store 取出完整 history（含工具訊息），
    這是主要機制。
    
    路徑 B — text summary injection（本函數）：
    將工具結果轉為文字摘要，注入到 input 中作為 context，
    提供雙重保險。
    
    返回新的 body bytes，如果不需要修改則返回 None。
    """
    prev_id = req_json.get("previous_response_id")
    if not prev_id:
        return None
    
    # 取得 upstream 的 base URL
    upstream_base = upstream_url.rsplit("/v1/responses", 1)[0]
    get_url = f"{upstream_base}/v1/responses/{prev_id}"
    
    try:
        async with sess.get(get_url, headers=fwd_headers) as resp:
            if resp.status != 200:
                logger.debug(f"[responses] Previous response {prev_id} not found (status={resp.status})")
                return None
            
            prev_resp = await resp.json()
            output_items = prev_resp.get("output", [])
            if not output_items:
                output_items = prev_resp.get("response", {}).get("output", [])
            
            # 從 tool items 中提取文字摘要
            tool_summaries: list[str] = []
            for item in output_items:
                item_type = item.get("type")
                if item_type == "function_call":
                    name = item.get("name", "unknown")
                    try:
                        args = json.loads(item.get("arguments", "{}"))
                        arg_preview = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
                    except (json.JSONDecodeError, TypeError):
                        arg_preview = item.get("arguments", "")[:80]
                    tool_summaries.append(f"[Previous turn] Tool called: {name}({arg_preview})")
                elif item_type == "function_call_output":
                    output = item.get("output", "")
                    # Truncate long outputs
                    if len(output) > 500:
                        output = output[:500] + "..."
                    tool_summaries.append(f"[Previous turn] Tool result: {output}")
            
            if not tool_summaries:
                logger.debug(f"[responses] No tool items to summarize from {prev_id}")
                return None
            
            summary_text = "\n".join(tool_summaries)
            
            # 注入到 input 的 user message 前面
            current_input = req_json.get("input", "")
            
            context_block = (
                "<tool_results_from_previous_turn>\n"
                f"{summary_text}\n"
                "</tool_results_from_previous_turn>\n\n"
            )
            
            if isinstance(current_input, str):
                new_input = context_block + current_input
            elif isinstance(current_input, list):
                # 找到 user message 並在前面插入
                new_input = []
                injected = False
                for item in current_input:
                    if not injected and isinstance(item, dict) and item.get("role") == "user":
                        orig_content = item.get("content", "")
                        new_item = dict(item)
                        new_item["content"] = context_block + orig_content
                        new_input.append(new_item)
                        injected = True
                    else:
                        new_input.append(item)
                if not injected:
                    new_input = [{"role": "user", "content": context_block}] + new_input
                current_input = new_input
            else:
                return None
            
            req_json["input"] = current_input
            new_body = json.dumps(req_json, ensure_ascii=False).encode("utf-8")
            
            logger.info(
                f"[responses] Injected {len(tool_summaries)} tool-result summaries "
                f"from {prev_id} into input (path B: text injection)"
            )
            logger.debug(
                f"[responses] Injected text:\n{context_block[:500]}"
            )
            
            return new_body
            
    except Exception as e:
        logger.warning(f"[responses] Failed to inject previous tool results: {e}")
        return None


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
