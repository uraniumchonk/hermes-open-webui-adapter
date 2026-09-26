"""
Completions Handler — 處理 /v1/chat/completions 端點。

1. 接收 Open WebUI 的 Chat Completions 請求
2. 執行 history sanitization（清理 <details> 污染 → native assistant + tool roles）
3. 轉發給 Hermes Gateway
4. 即時轉換 SSE stream（stream_enhance.transform_stream，enhance-v2）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict

import aiohttp
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse, Response
import special_tags
import tool_history_format
from runtime import CONFIG
from stream_enhance import transform_stream
from tool_history_structured import sanitize_messages_structured

logger = logging.getLogger(__name__)


# ── History Sanitization (Anti-pollution) ─────────────────
#
# 問題：hermes_tool_filter 注入的 <details> 標籤以 delta.content 純文字形式
# 進入 Open WebUI 的對話歷史。下次請求時，這些標籤會完整出現在模型的 prompt 中，
# 導致模型模仿輸出 <details> 格式，形成污染反饋迴圈。
#
# 解決：在把請求轉發到 upstream 之前，掃描 messages 中的 assistant content，
# 把 <details type="tool_calls"> 區塊轉換為安全的格式。
#
# 配置：config.yaml 中的 enable_history_sanitization, sanitization_result_max_length
# 只支援 structured 格式（OpenAI native tool role messages）


def sanitize_request_messages(messages: list) -> list:
    """
    Scan and sanitize all messages in the request to prevent <details> pollution.
    Only processes assistant role content.

    Runtime path: structured only (OpenAI native tool role).
    flat/legacy were removed in 61a58dc — see git ≤877fdb7 + README templates.
    """
    if not messages:
        return messages

    enabled, _max_len, fmt = tool_history_format._get_sanitization_config(CONFIG)
    if not enabled:
        return messages

    if fmt != "structured":
        # Do not silently pretend flat still works.
        logger.warning(
            "[history] tool_history_format=%r is not in runtime "
            "(removed 61a58dc; restore from git ≤877fdb7). Using structured.",
            fmt,
        )

    return sanitize_messages_structured(messages, CONFIG)


async def handle_completions_request(
    request: Request,
    upstream_url: str,
    fwd_headers: Dict[str, str],
    body: bytes,
    req_json: Dict[str, Any],
    sess: aiohttp.ClientSession,
    upstream_port: str,
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

    completion_id = f"chatcmpl-{int(time.time()*1000)}"
    created_ts = int(time.time())

    # ✅ Task 1: strip 思考內容（OWUI 把思考區域組裝回傳 LLM 時砍掉，防污染反饋迴圈）
    # ✅ History Sanitization: 在轉發前清理 messages 中的 <details> 標籤
    if "messages" in req_json and isinstance(req_json["messages"], list):
        req_json["messages"] = special_tags.strip_thinking_content(req_json["messages"])
        req_json["messages"] = sanitize_request_messages(req_json["messages"])

    body = json.dumps(req_json, ensure_ascii=False).encode("utf-8")

    # --- Streaming path (chat completions with stream=true) ---
    if stream_flag and "chat/completions" in original_path:
        async def generate():
            upstream_resp = None
            try:
                # Prefer context-managed response so connection is always released.
                # Keep explicit close paths for CancelledError / partial failure.
                upstream_resp = await sess.post(
                    upstream_url, data=body, headers=fwd_headers
                )
                logger.info(
                    f"[port={upstream_port}] Proxied chat completions, "
                    f"upstream status={upstream_resp.status}"
                )
                
                # ✅ 關鍵修復：使用 queue 解耦讀取和寫入，避免 backpressure
                # 當下游客戶端讀取慢時，yield 會阻塞，但讀取任務在背景運行
                queue = asyncio.Queue(maxsize=1000)  # 限制記憶體使用
                read_task = None
                
                async def reader_task():
                    """背景任務：持續從 upstream 讀取並轉換"""
                    try:
                        async for chunk in transform_stream(
                            upstream_resp.content, model, completion_id, created_ts,
                        ):
                            await queue.put(chunk)
                        # 標記完成
                        await queue.put(None)
                    except Exception as e:
                        logger.error(f"[queue-reader] Error: {type(e).__name__}: {e}")
                        await queue.put(None)
                
                # 啟動背景讀取任務（保留 read_task 參照：event loop 只持有 task 的弱參照，
                # 沒人引用可能在執行中被 GC——pyflakes 報 unused 也別刪）
                read_task = asyncio.create_task(reader_task())
                
                # 從 queue 讀取並 yield - 這不會阻塞 upstream 讀取
                while True:
                    chunk = await queue.get()
                    if chunk is None:  # 完成標記
                        break
                    yield chunk
            except asyncio.CancelledError:
                logger.info(f"[port={upstream_port}] Client disconnected, closing upstream gracefully")
                raise
            except aiohttp.ServerDisconnectedError:
                logger.info(f"[port={upstream_port}] Upstream disconnected")
            except aiohttp.ClientError as e:
                logger.warning(f"[port={upstream_port}] Client error: {type(e).__name__}: {e}")
                yield b'data: {"error":{"message":"Internal proxy error","type":"proxy_error","code":"upstream_failure"}}\n\n'
            except Exception as e:
                logger.error(f"[port={upstream_port}] Proxy error: {type(e).__name__}: {e}", exc_info=True)
                yield b'data: {"error":{"message":"Internal proxy error","type":"proxy_error","code":"upstream_failure","detail": "' + str(e).encode('utf-8', errors='replace') + b'"}}\n\n'
            finally:
                if upstream_resp is not None and not upstream_resp.closed:
                    upstream_resp.close()
                    # Release connection back to pool promptly (aiohttp best practice)
                    try:
                        await upstream_resp.release()
                    except Exception:
                        pass

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # 禁用 Nginx 緩衝
                "X-Proxy-Buffering": "no",  # 禁用其他代理緩衝
                "Flush-After-Header": "true",  # 強制立即刷出
                "Content-Encoding": "identity",  # 禁用壓縮（避免代理緩衝壓縮數據）
                # ✅ 防火牆優化：額外頭部強制代理不緩衝
                "X-Content-Type-Options": "nosniff",
                "X-Permitted-Cross-Domain-Policies": "none",
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
        # The shared session has no lifetime cap (long agent tasks); bound
        # the non-streaming path explicitly. The wire is silent for the
        # whole agent run (body arrives at the end), so both total and
        # sock_read are generous.
        _non_stream_timeout = aiohttp.ClientTimeout(total=3600, connect=10, sock_read=3600)
        async with sess.request(
            method, upstream_url, data=body, headers=fwd_headers,
            timeout=_non_stream_timeout,
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
