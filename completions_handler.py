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

# ── Test Mode Trigger ──────────────────────────────────────
# 當最後一則 user message 包含這個關鍵字時，觸發測試模式。
TEST_MODE_TRIGGER = "[TEST_TOOL_CARDS]"


def _check_test_mode(req_json: Dict[str, Any]) -> bool:
    """
    檢查請求是否為測試模式。
    條件：最後一則 user message 的內容包含 TEST_MODE_TRIGGER。
    """
    messages = req_json.get("messages", [])
    if not messages:
        return False
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return TEST_MODE_TRIGGER in content
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and TEST_MODE_TRIGGER in part.get("text", ""):
                        return True
            break
    return False


def _handle_test_mode(completion_id: str, created: int, model: str) -> StreamingResponse:
    """
    測試模式：直接回傳預先寫好的 tool card 樣本，不轉發 upstream。
    """
    from test_mode import generate_test_stream
    return StreamingResponse(
        generate_test_stream(completion_id, created, model),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Proxy-Buffering": "no",
            "Flush-After-Header": "true",
            "Content-Encoding": "identity",
        },
    )


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
    - 測試模式（直接回傳預先寫好的樣本）
    - 串流模式（SSE + enhance-v2 轉換）
    - 非串流模式（直接透傳）
    """
    # 🔍 臨時 DEBUG：記錄請求結構（確認 Open WebUI 發送什麼欄位）
    _debug_keys = list(req_json.keys())
    _meta = req_json.get("metadata", {})
    _has_chat_id = "chat_id" in req_json or "chatId" in req_json or "chatId" in str(_meta)
    _msg_count = len(req_json.get("messages", []))
    _stream_opts = req_json.get("stream_options", {})
    _num_ctx = req_json.get("num_ctx", "")
    # 提取 messages 的 role 分佈
    _roles = [m.get("role","?") for m in req_json.get("messages",[])]
    # 提取第一個 message 的 keys（system prompt）
    _first_msg_keys = list(req_json.get("messages",[{}])[0].keys()) if _msg_count > 0 else []
    # 計算每個 message 的 content 長度
    _msg_lens = [len(str(m.get("content",""))) for m in req_json.get("messages",[])]
    # 檢查是否有 X-Hermes-Session-Id 已經存在
    _has_hermes_sid = bool(request.headers.get("X-Hermes-Session-Id", "").strip())
    logger.info(
        f"[DEBUG-request] keys={_debug_keys} | stream_options={_stream_opts} "
        f"| num_ctx={_num_ctx} | metadata_keys={list(_meta.keys())} "
        f"| messages={_msg_count} roles={_roles} "
        f"| msg_lens={_msg_lens} | first_msg_keys={_first_msg_keys} "
        f"| chat_id={_has_chat_id} | hermes_sid={_has_hermes_sid} | model={req_json.get('model','')}"
    )

    model = req_json.get("model", "hermes-agent")
    stream_flag = req_json.get("stream", True)
    original_path = request.scope.get("path", "")

    completion_id = f"chatcmpl-{int(time.time()*1000)}"
    created_ts = int(time.time())

    # ── 🧪 Test Mode: 直接回傳測試樣本，不轉發 upstream ──
    if _check_test_mode(req_json):
        logger.info(f"[test-mode] Triggered! Sending tool card samples directly.")
        return _handle_test_mode(completion_id, created_ts, model)

    # ✅ History Sanitization: 在轉發前清理 messages 中的 <details> 標籤
    if "messages" in req_json and isinstance(req_json["messages"], list):
        req_json["messages"] = sanitize_request_messages(req_json["messages"])

    body = json.dumps(req_json, ensure_ascii=False).encode("utf-8")

    # Detect client type from User-Agent to decide if we strip <details> tags
    user_agent = request.headers.get("user-agent", "").lower()
    strip_details = "dart" in user_agent or "conduit" in user_agent

    # --- Streaming path (chat completions with stream=true) ---
    if stream_flag and "chat/completions" in original_path:
        completion_id = f"chatcmpl-{int(time.time()*1000)}"
        created_ts = int(time.time())

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
