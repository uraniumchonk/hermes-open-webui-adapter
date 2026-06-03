#!/usr/bin/env python3
"""
Hermes SSE Tool Card Enhancer Proxy (Multi-Tenant Router)

在 Open WebUI 和多個 Hermes Gateway profiles 之間的透明代理路由器。

路由规则：
  /30000/v1/*  → http://127.0.0.1:30000/v1/*  (default profile)
  /30001/v1/*  → http://127.0.0.1:30001/v1/*  (coder profile)
  /30002/v1/*  → http://127.0.0.1:30002/v1/*  (analyst profile)
  /30003/v1/*  → http://127.0.0.1:30003/v1/*  (trader profile)

SSE Transform：攔截 hermes.tool.progress 事件，在 completed 時注入
<details done="true"> 標籤，讓 Open WebUI 正確更新工具卡片狀態。

Systemd service: hermes-tool-filter.service
"""

import asyncio
import json
import html
import logging
import os
import re
import time
from typing import Dict, Optional, AsyncGenerator

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response, JSONResponse

# ── Logging ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("tool-filter")

# ── App ───────────────────────────────────────────────────
APP = FastAPI(title="Hermes Tool Card Enhancer Router")

BIND_HOST = "0.0.0.0"
BIND_PORT = 9099

# Port routing table: path prefix -> upstream base URL
PORT_MAP: Dict[str, str] = {
    "30000": "http://127.0.0.1:30000",
    "30001": "http://127.0.0.1:30001",
    "30002": "http://127.0.0.1:30002",
    "30003": "http://127.0.0.1:30003",
}

# Default upstream if no port prefix matched
DEFAULT_UPSTREAM = PORT_MAP["30000"]

# ── Emoji Mapping ─────────────────────────────────────────

TOOL_EMOJI: Dict[str, str] = {
    "terminal": "💻",
    "read_file": "📖",
    "write_file": "✍️",
    "patch": "🩹",
    "search_files": "🔎",
    "execute_code": "🐍",
    "delegate_task": "🔀",
    "clarify": "❓",
    "todo": "📋",
    "web_search": "🌐",
    "brave_web_search": "🌐",
    "memory": "🧠",
    "skill_view": "🛠️",
    "session_search": "🔍",
    "process": "⚙️",
}
DEFAULT_EMOJI = "🔧"


def get_tool_emoji(tool: str) -> str:
    return TOOL_EMOJI.get(tool, DEFAULT_EMOJI)


# ── Detail Tag Builder ────────────────────────────────────

def build_details_tag(
    tool_call_id: str,
    tool_name: str,
    emoji: str,
    label: str,
    done: bool,
) -> str:
    """建立 <details type="tool_calls"> 標籤供 Open WebUI 渲染。"""
    safe_name = html.escape(tool_name)
    if done:
        return (
            f'<details type="tool_calls" done="true" id="{tool_call_id}" '
            f'name="{safe_name}">'
            f'\n<summary>{emoji} Done</summary>'
            f'</details>\n'
        )
    else:
        return (
            f'<details type="tool_calls" done="false" id="{tool_call_id}" '
            f'name="{safe_name}">'
            f'\n<summary>{emoji} Running... {label}</summary>'
            f'</details>\n'
        )


def make_sse_line(data_obj: dict) -> bytes:
    """序列化為 SSE data 行。"""
    return f"data: {json.dumps(data_obj, ensure_ascii=False)}\n\n".encode("utf-8")


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


# ── SSE Stream Transformer ────────────────────────────────

# 自動分割閾值（字元數），0 表示關閉
# 注意: 目前 Conduit APP 不支援 session.split 事件，開啟後會導致 stream 中斷
AUTO_SPLIT_THRESHOLD = int(os.environ.get("AUTO_SPLIT_THRESHOLD", "0"))


def replace_done_false(frame: str) -> str:
    """
    Replace done=false with done=true in <details tags.
    Handles any level of quote escaping by finding 'done=' then 'false' after it.
    """
    start = frame.find("done=")
    if start < 0:
        return frame
    # Find 'false' after 'done=' (within next 20 chars)
    search_region = frame[start:start + 20]
    false_pos = search_region.find("false")
    if false_pos < 0:
        return frame
    # Build the replacement
    before = frame[:start + false_pos + 5]  # 'done=' + 'false' position
    after = frame[start + false_pos + 5:]   # rest after 'false'
    # Find the actual 'false' in the original frame
    actual_false_pos = start + false_pos
    result = frame[:actual_false_pos] + "true" + frame[actual_false_pos + 5:]
    return result


# Details-tag regex: matches <details ...>...</details> including newlines
_DETAILS_RE = re.compile(r'<details[^>]*>.*?</details>', re.DOTALL)


def _extract_tool_info(details_html: str) -> dict:
    """
    從 <details> 標籤中提取工具資訊。
    
    Returns: {name, emoji, label, done, id}
    """
    # 提取 name 屬性
    name_match = re.search(r'name="([^"]*)"', details_html)
    name = name_match.group(1) if name_match else "unknown"
    
    # 提取 done 屬性
    done_match = re.search(r'done="([^"]*)"', details_html)
    done = done_match and done_match.group(1) == "true"
    
    # 提取 id 屬性
    id_match = re.search(r'id="([^"]*)"', details_html)
    tc_id = id_match.group(1) if id_match else ""
    
    # 提取 <summary> 內容
    summary_match = re.search(r'<summary>(.*?)</summary>', details_html, re.DOTALL)
    summary = summary_match.group(1).strip() if summary_match else ""
    
    # 從 summary 中提取 emoji 和 label
    # 格式: "💻 Running... echo hello" 或 "✅ Done"
    emoji = ""
    label = summary
    
    # 嘗試提取開頭的 emoji（包含 variation selector \uFE0F）
    # emoji 可能後面跟 \uFE0F (VS15) 或 \u200D (ZWS) 等組合字元
    emoji_pattern = re.match(r'^([\U0001F300-\U0001F9FF\U00002600-\U000027BF\U00002700-\U000027BF][\uFE0F\u200D\u20E3]*\s*)', summary)
    
    if emoji_pattern:
        emoji = emoji_pattern.group(1).strip()
        label = summary[emoji_pattern.end():].strip()
    
    # 從 label 中移除 "Running..." 或 "Done"
    label = re.sub(r'^(Running\.\.\.|Done)\s*', '', label)
    
    # 如果沒有 emoji，使用 TOOL_EMOJI 映射
    if not emoji:
        emoji = get_tool_emoji(name)
    
    return {
        "name": name,
        "emoji": emoji,
        "label": label,
        "done": done,
        "id": tc_id,
    }


def _format_tool_markdown(tool_info: dict) -> str:
    """
    將工具資訊格式化為 Markdown 純文字。
    
    格式: `**💻 terminal**` ┃ `echo hello`
    """
    emoji = tool_info.get("emoji", "🔧")
    name = tool_info.get("name", "unknown")
    label = tool_info.get("label", "")
    done = tool_info.get("done", False)
    
    # 狀態標記
    status = "✅" if done else "🔄"
    
    # 移除前綴標記 (如 "⌨️ Running..."、"🌐 Running..."、"🖥️ Running..." 等)
    # 這些是 Hermes 加的顯示前綴，實際參數在後面
    import re
    # 移除 "emoji Running... " 格式的前綴
    label = re.sub(r'^[\U0001F000-\U0001FFFF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF]*\s*Running\.\.\.\s*', '', label)
    # 移除純 "Running... " 前綴
    label = re.sub(r'^Running\.\.\.\s*', '', label)
    
    # 截斷過長的 label (最大 50 字元)
    max_label_len = 50
    if len(label) > max_label_len:
        label = label[:max_label_len] + "..."
    
    # 構建 Markdown 格式
    # 使用 inline code 包裹工具名稱，用 backtick 包裹參數
    if label:
        return f"**{emoji} {name}** {status} `{label}`\n"
    else:
        return f"**{emoji} {name}** {status}\n"


def _strip_details_from_content(frame: str) -> str:
    """
    Parse an SSE frame's JSON data, strip <details>...</details> from
    choices[0].delta.content, and re-serialize. Returns the modified frame.
    
    This prevents Conduit from rendering raw HTML and polluting the
    conversation context with tool-call markup.
    
    優化版本：使用 Markdown 格式顯示工具資訊，包含：
    - 工具名稱（加粗）
    - emoji 標記
    - 參數（inline code）
    - 狀態標記（✅/🔄）
    """
    # Extract ONLY the data: line(s) from the frame
    # Frame may contain "event: ..." lines before "data: ..."
    lines = frame.strip().split("\n")
    data_lines = []
    prefix_lines = []
    
    for line_item in lines:
        if line_item.startswith("data:") or line_item == "data:":
            data_lines.append(line_item)
        else:
            prefix_lines.append(line_item)
    
    if not data_lines:
        return frame
    
    # Reconstruct data_str from data lines
    data_str_parts = []
    for dl in data_lines:
        if dl == "data:":
            data_str_parts.append("")
        elif dl.startswith("data: "):
            data_str_parts.append(dl[6:])
        else:
            data_str_parts.append(dl[5:])
    
    data_str = "\n".join(data_str_parts)
    
    try:
        payload = json.loads(data_str)
    except json.JSONDecodeError:
        return frame

    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) == 0:
        return frame

    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return frame

    content = delta.get("content")
    if not isinstance(content, str) or "<details" not in content:
        return frame

    # 提取所有 <details> 區塊
    details_blocks = _DETAILS_RE.findall(content)
    if not details_blocks:
        return frame

    # 對每個 <details> 區塊生成 Markdown 格式
    tool_lines = []
    for block in details_blocks:
        tool_info = _extract_tool_info(block)
        tool_lines.append(_format_tool_markdown(tool_info))
    
    # 將所有工具行合併，用換行分隔
    cleaned = "\n".join(tool_lines)
    
    # 如果原始 content 還有其他文字，保留它
    cleaned_content = _DETAILS_RE.sub("", content).strip()
    if cleaned_content:
        cleaned = cleaned_content + "\n" + cleaned

    delta["content"] = cleaned

    # Reconstruct the frame
    # Preserve prefix lines (e.g., event: ...)
    if prefix_lines:
        reconstructed = "\n".join(prefix_lines) + "\n"
    else:
        reconstructed = ""
    
    # Add data: prefix (with space)
    reconstructed += "data: " + json.dumps(payload, ensure_ascii=False)
    
    return reconstructed

async def transform_stream(
    reader: asyncio.StreamReader,
    model: str,
    completion_id: str,
    created: int,
    upstream_port: str,
    strip_details: bool = False,
) -> AsyncGenerator[bytes, None]:
    """
    從 Hermes 上游讀取 SSE stream，即時轉換 hermes.tool.progress 事件。

    對於 status=completed 的事件，額外注入一個 <details done="true"> 的
    delta.content chunk，讓 Open WebUI 能正確更新工具卡片的狀態。
    
    自動分割功能：當 content 累積超過閾值時，自動結束當前 stream 並繼續。
    """

    # Track tool states: toolCallId -> {tool, emoji, label}
    tool_states: Dict[str, dict] = {}

    done_received = False
    split_done = False  # 是否已發送過分割標記

    buffer = ""
    
    # 自動分割計數器
    accumulated_content = ""
    has_split = False

    while True:
        line = await reader.readline()

        # Empty line means end of connection
        if not line:
            break

        buffer += line.decode("utf-8", errors="replace")

        # Process complete SSE frames (terminated by \n\n)
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)

            # Check for [DONE] signal early - stop processing after it
            if "[DONE]" in frame and not done_received:
                yield (frame + "\n\n").encode("utf-8")
                done_received = True
                return

            # Parse the frame - support both "data:" and "data: " formats
            lines = frame.strip().split("\n")
            event_type = None
            data_lines = []

            for line_item in lines:
                if line_item.startswith("event: "):
                    event_type = line_item[7:].strip()
                elif line_item.startswith("data:") or line_item == "data:":
                    # Support multiline data: collect all data lines
                    data_lines.append(line_item[5:].lstrip(" "))

            # Join multiline data with newlines (SSE spec)
            data_str = "\n".join(data_lines) if data_lines else None

            # Skip hermes.tool.progress events entirely - Conduit cannot parse them
            if event_type == "hermes.tool.progress":
                if data_str:
                    try:
                        payload = json.loads(data_str)
                        tc_id = payload.get("toolCallId", "")
                        status = payload.get("status", "")
                        tool = payload.get("tool", "unknown")

                        if status == "running":
                            tool_states[tc_id] = {
                                "tool": tool,
                                "emoji": payload.get("emoji", ""),
                                "label": payload.get("label", tool),
                            }
                        elif status == "completed":
                            tool_states.pop(tc_id, None)
                    except json.JSONDecodeError:
                        pass
                # Do NOT yield - skip this frame
                continue

            # 一律 strip <details> tags 並替換為可見文字。
            # Hermes Gateway embeds <details type="tool_calls"> in delta.content,
            # 但 Conduit 無法渲染，Open WebUI 的卡片也殘，所以一律替換。
            if data_str and ("<details" in data_str or "<details" in frame):
                modified_frame = _strip_details_from_content(frame)
            else:
                modified_frame = frame
            
            # 自動分割檢查
            if AUTO_SPLIT_THRESHOLD > 0 and not has_split:
                try:
                    payload = json.loads(data_str) if data_str else {}
                    choices = payload.get("choices")
                    if isinstance(choices, list) and len(choices) > 0:
                        delta = choices[0].get("delta")
                        if isinstance(delta, dict):
                            content = delta.get("content", "")
                            if isinstance(content, str) and content:
                                accumulated_content += content
                                
                                # 檢查是否超過閾值
                                if len(accumulated_content) >= AUTO_SPLIT_THRESHOLD:
                                    has_split = True
                                    # 發送 [DONE] 結束當前 stream
                                    yield b'data: {"id": "' + completion_id.encode() + b'", "object": "chat.completion.chunk", "choices": [{"index": 0, "finish_reason": "length"}]}\n\n'
                                    yield b'data: [DONE]\n\n'
                                    # 發送分割事件
                                    split_event = {
                                        "type": "session.split",
                                        "message": "會話自動分割，繼續中...",
                                        "chars_processed": len(accumulated_content)
                                    }
                                    yield b'event: session.split\n'
                                    yield b'data: ' + json.dumps(split_event, ensure_ascii=False).encode() + b'\n\n'
                                    # 清空計數器，繼續處理後續內容
                                    accumulated_content = ""
                except (json.JSONDecodeError, Exception):
                    pass
            
            yield (modified_frame + "\n\n").encode("utf-8")


    # Flush remaining buffer with proper SSE termination
    if buffer.strip():
        # Ensure the residual buffer ends with \n\n for proper SSE framing
        cleaned = buffer.rstrip("\r\n")
        if cleaned:
            yield (cleaned + "\n\n").encode("utf-8")


# ── Shared aiohttp session ────────────────────────────────

_http_session: Optional[aiohttp.ClientSession] = None


async def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        timeout = aiohttp.ClientTimeout(total=600, connect=10, sock_read=600)
        _http_session = aiohttp.ClientSession(timeout=timeout)
    return _http_session


# ── Route: Catch-all proxy ────────────────────────────────

@APP.api_route("/{port_prefix}/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_with_transform(request: Request, port_prefix: str, rest: str):
    """
    Main proxy route with SSE transformation for chat completions.

    Matches paths like /30000/v1/chat/completions, /30001/v1/models, etc.
    """
    upstream_port = port_prefix
    original_path = f"/{port_prefix}/{rest}"
    upstream_url = resolve_upstream(original_path)

    body = await request.body()

    # Build forwarded headers
    fwd_headers = {}
    for hn, hv in request.headers.items():
        hl = hn.lower()
        if hl in ("authorization", "content-type"):
            fwd_headers[hn] = hv

    # Parse request body for model name
    try:
        req_json = json.loads(body) if body else {}
    except json.JSONDecodeError:
        req_json = {}

    model = req_json.get("model", "hermes-agent")
    stream_flag = req_json.get("stream", True)

    # Detect client type from User-Agent to decide if we strip <details> tags
    user_agent = request.headers.get("user-agent", "").lower()
    strip_details = "dart" in user_agent or "conduit" in user_agent
    # Open WebUI (user-agent contains "open-webui" or is a browser) keeps <details>

    completion_id = f"chatcmpl-{int(time.time()*1000)}"
    created_ts = int(time.time())

    sess = await get_session()

    # --- Streaming path (chat completions with stream=true) ---
    if stream_flag and "chat/completions" in original_path:

        async def generate():
            try:
                async with sess.post(
                    upstream_url, data=body, headers=fwd_headers
                ) as resp:
                    logger.info(
                        f"[port={upstream_port}] Proxied chat completions, "
                        f"upstream status={resp.status}, "
                        f"strip_details={strip_details} (UA: {user_agent[:50]})"
                    )
                    async for chunk in transform_stream(
                        resp.content, model, completion_id, created_ts,
                        upstream_port, strip_details,
                    ):
                        yield chunk
            except aiohttp.ServerDisconnectedError:
                # Upstream disconnected after auto-split — this is expected, not an error
                logger.info(f"[port={upstream_port}] Upstream disconnected (expected after auto-split)")
            except aiohttp.ClientError as e:
                # Other client errors (connection reset, timeout, etc.)
                logger.warning(f"[port={upstream_port}] Client error: {e}")
            except Exception as e:
                logger.error(f"[port={upstream_port}] Proxy error: {type(e).__name__}: {e}")
                err = {
                    "error": {
                        "message": str(e),
                        "type": "proxy_error",
                        "code": "upstream_failure",
                    }
                }
                yield f"data: {json.dumps(err)}\n\n".encode()

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
    try:
        async with sess.request(
            method, upstream_url, data=body, headers=fwd_headers
        ) as resp:
            resp_body = await resp.read()
            try:
                parsed = json.loads(resp_body) if resp_body else {}
            except json.JSONDecodeError:
                parsed = {}
            return JSONResponse(
                content=parsed,
                status_code=resp.status,
            )
    except json.JSONDecodeError:
        return Response(
            content=await resp.read() if resp_body else b"",
            status_code=resp.status,
            headers=dict(resp.headers),
        )


@APP.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_default(request: Request, rest: str):
    """
    Fallback proxy route for paths WITHOUT port prefix.
    Routes to default upstream (30000).

    Matches /v1/models, /v1/chat/completions, etc.
    """
    original_path = f"/{rest}"
    upstream_url = resolve_upstream(original_path)

    body = await request.body()

    fwd_headers = {}
    for hn, hv in request.headers.items():
        hl = hn.lower()
        if hl in ("authorization", "content-type"):
            fwd_headers[hn] = hv

    try:
        req_json = json.loads(body) if body else {}
    except json.JSONDecodeError:
        req_json = {}

    model = req_json.get("model", "hermes-agent")
    stream_flag = req_json.get("stream", True)

    # Detect client type from User-Agent to decide if we strip <details> tags
    user_agent = request.headers.get("user-agent", "").lower()
    strip_details = "dart" in user_agent or "conduit" in user_agent

    completion_id = f"chatcmpl-{int(time.time()*1000)}"
    created_ts = int(time.time())

    sess = await get_session()
    upstream_port = "30000"  # default

    if stream_flag and "chat/completions" in original_path:

        async def generate():
            try:
                async with sess.post(
                    upstream_url, data=body, headers=fwd_headers
                ) as resp:
                    logger.info(
                        f"[port={upstream_port}] Proxied (default) chat completions, "
                        f"upstream status={resp.status}, "
                        f"strip_details={strip_details} (UA: {user_agent[:50]})"
                    )
                    async for chunk in transform_stream(
                        resp.content, model, completion_id, created_ts,
                        upstream_port, strip_details,
                    ):
                        yield chunk
            except aiohttp.ServerDisconnectedError:
                # Upstream disconnected after auto-split — this is expected, not an error
                logger.info(f"[port={upstream_port}] Upstream disconnected (expected after auto-split)")
            except aiohttp.ClientError as e:
                # Other client errors (connection reset, timeout, etc.)
                logger.warning(f"[port={upstream_port}] Client error: {e}")
            except Exception as e:
                logger.error(f"[port={upstream_port}] Proxy error: {type(e).__name__}: {e}")
                err = {
                    "error": {
                        "message": str(e),
                        "type": "proxy_error",
                        "code": "upstream_failure",
                    }
                }
                yield f"data: {json.dumps(err)}\n\n".encode()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming passthrough
    method = request.method.upper()
    async with sess.request(
        method, upstream_url, data=body, headers=fwd_headers
    ) as resp:
        resp_body = await resp.read()
        try:
            parsed = json.loads(resp_body) if resp_body else {}
        except json.JSONDecodeError:
            parsed = {}
        return JSONResponse(content=parsed, status_code=resp.status)


# ── Health Check ───────────────────────────────────────────

@APP.get("/health")
async def health():
    return {
        "status": "ok",
        "ports": {p: u for p, u in PORT_MAP.items()},
        "default_upstream": DEFAULT_UPSTREAM,
    }


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
    uvicorn.run(APP, host=BIND_HOST, port=BIND_PORT, log_level="info")
