#!/usr/bin/env python3
"""
Hermes SSE Tool Card Enhancer Proxy (Multi-Tenant Router)

在 Open WebUI 和多個 Hermes Gateway profiles 之間的透明代理路由器。

路由规则（由 config.yaml 中的 upstreams 配置決定）：
  /30000/v1/*  → http://127.0.0.1:30000/v1/*  (通用聊天)
  /30001/v1/*  → http://127.0.0.1:30001/v1/*  (程式開發專家)
  /30002/v1/*  → http://127.0.0.1:30002/v1/*  (資料與研究)
  /30003/v1/*  → http://127.0.0.1:30003/v1/*  (交易與市場)

SSE Transform：攔截 hermes.tool.progress 事件，在 completed 時注入
<details done="true"> 標籤，讓 Conduit APP 正確顯示工具卡片狀態。

配置：config.yaml (優先) 或 .env (後備)
Systemd service: hermes-tool-filter.service
"""

import asyncio
import json
import html
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, AsyncGenerator, List

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response, JSONResponse

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# ── Logging ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("tool-filter")

# ── Configuration ──────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.yaml"
CONFIG: Dict[str, Any] = {}

def _load_config() -> Dict[str, Any]:
    """
    載入配置。優先順序: config.yaml > .env > 預設值
    """
    cfg: Dict[str, Any] = {}

    # 1. 載入 config.yaml
    if CONFIG_PATH.exists() and HAS_YAML:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                yaml_cfg = yaml.safe_load(f) or {}
            logger.info(f"Loaded config from {CONFIG_PATH}")
            cfg.update(yaml_cfg)
        except Exception as e:
            logger.warning(f"Failed to load config.yaml: {e}")
    elif CONFIG_PATH.exists():
        logger.warning("config.yaml exists but PyYAML is not installed. Install with: pip install pyyaml")

    # 2. .env 環境變數覆蓋
    if os.environ.get("TOOL_MODE"):
        cfg["tool_mode"] = os.environ["TOOL_MODE"]
    if os.environ.get("AUTO_SPLIT_THRESHOLD"):
        cfg["auto_split_threshold"] = int(os.environ["AUTO_SPLIT_THRESHOLD"])
    if os.environ.get("BIND_PORT"):
        cfg["bind_port"] = int(os.environ["BIND_PORT"])
    if os.environ.get("BIND_HOST"):
        cfg["bind_host"] = os.environ["BIND_HOST"]

    return cfg

CONFIG = _load_config()

# ── App ───────────────────────────────────────────────────
APP = FastAPI(title="Hermes Tool Card Enhancer Router")

BIND_HOST = CONFIG.get("bind_host", "0.0.0.0")
BIND_PORT = CONFIG.get("bind_port", 9099)

# Port routing table: path prefix -> upstream base URL
# Loaded from config.yaml "upstreams" section, with sensible defaults.
PORT_MAP: Dict[str, str] = {}

def _build_port_map() -> Dict[str, str]:
    """Build PORT_MAP from config or use defaults."""
    upstreams = CONFIG.get("upstreams", {})
    if upstreams:
        return {str(k): str(v) for k, v in upstreams.items()}
    # Default: Hermes's built-in profiles (default, coder, analyst, trader)
    return {
        "30000": "http://127.0.0.1:30000",
        "30001": "http://127.0.0.1:30001",
        "30002": "http://127.0.0.1:30002",
        "30003": "http://127.0.0.1:30003",
    }


PORT_MAP = _build_port_map()

# Default upstream if no port prefix matched — pick the first one or fallback
DEFAULT_UPSTREAM = next(iter(PORT_MAP.values())) if PORT_MAP else "http://127.0.0.1:30000"

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

TOOL_MODE = CONFIG.get("tool_mode", "enhance")
AUTO_SPLIT_THRESHOLD = CONFIG.get("auto_split_threshold", 0)

logger.info(f"Configuration loaded: tool_mode={TOOL_MODE}, auto_split={AUTO_SPLIT_THRESHOLD}")

def _strip_details_from_content(frame: str) -> str:
    """
    Parse an SSE frame's JSON data, preserve <details>...</details> for
    Conduit APP rendering, and re-serialize. Returns the modified frame.

    Conduit APP has a complete <details> rendering system:
    - <details type="tool_calls"> is rendered as expandable tool cards
    - ToolCallsParser.sanitizeForApi() strips them before sending to LLM
    - So we keep the raw <details> tags intact for UI rendering

    We only enhance the <details> tags by adding missing attributes
    (arguments, result) when available from hermes.tool.progress events.
    """
    # Simply return the frame as-is — Conduit handles <details> natively
    return frame


# ── Tool Mode Handlers ─────────────────────────────────────


def _encode_detail_attribute(value: Any) -> str:
    """
    Encode a value as a <details> attribute:
    JSON encode -> HTML escape (for safe attribute embedding).
    """
    if not value:
        return ""
    json_str = json.dumps(value, ensure_ascii=False)
    return html.escape(json_str, quote=True)


def _build_completion_details(tool_name: str, label: str = "", result: str = "") -> str:
    """
    Build a complete <details> tag for a completed tool call.
    
    - 確保 name 屬性正確（不會為空）
    - 使用 label 作為 input 參數顯示（放在 <arguments> 標籤內）
    - 結果放在 <result> 標籤內（避免 HTML 實體編碼問題）
    - 結果截斷（最多 5000 字元）
    """
    safe_name = html.escape(tool_name) if tool_name else "unknown"
    
    attrs = f'type="tool_calls" done="true" name="{safe_name}"'
    
    inner = "\n<summary>Done</summary>"
    
    if label:
        # arguments 放在標籤內，用 html.escape 避免 XSS
        inner += f"\n<arguments>{html.escape(label)}</arguments>"
    
    if result:
        # result 放在標籤內，用 html.escape 避免 XSS
        truncated = result[:5000] + ("..." if len(result) > 5000 else "")
        inner += f"\n<result>{html.escape(truncated)}</result>"
    
    return f'<details {attrs}>{inner}\n</details>\n'


def _build_content_chunk(content: str) -> bytes:
    """Build an SSE data: line with delta.content."""
    payload = {"choices": [{"delta": {"content": content}}]}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def handle_tool_completion(tool_name: str, label: str = "", result: str = "") -> bytes:
    """Build a completion <details> chunk to inject."""
    details = _build_completion_details(tool_name, label, result)
    return _build_content_chunk(details)


# ── Finish chunk builder ─────────────


def _build_finish_chunk(
    completion_id: str, created: int, model: str,
    finish_reason: str, usage: Optional[dict] = None
) -> bytes:
    """Build a finish chunk."""
    chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": finish_reason,
        }],
    }
    if usage:
        chunk["usage"] = usage
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


# ── Enhance-v2: Blocking Translation Mode ─────────────────
# 
# 核心概念：
# 1. 收到 running 事件 → 開始緩衝後續 content
# 2. 收到 completed 事件 → 輸出標準 tool_calls delta + tool role + 緩衝 content
# 3. 這讓 Open WebUI 能正確儲存完整的 conversation history

class ToolCallBuffer:
    """
    輕量級工具狀態追蹤器（for enhance-v2）。
    
    ⚠️ 重要修正：
    - content 正常即時串流（不緩衝）
    - 只在 tool completed 時，注入 <details type="tool_calls" done="true" arguments="..." result="...">
    - **絕對不 emit delta.tool_calls 或 role:"tool"** 
      → 否則 Open WebUI 會觸發 client-side tool execution loop，
        造成「會話重跑 + 一口氣出全部調用 + 模型失智」
    
    這是 Open WebUI 官方對「server-side tool execution」（像 Hermes Agent）的推薦做法。
    詳見 Pipes 文件。
    """
    
    def __init__(self):
        # 追蹤正在執行的工具: tc_id -> {tool, emoji, label, arguments}
        self.active_tools: Dict[str, dict] = {}
    
    def on_tool_running(self, tc_id: str, payload: dict) -> None:
        """工具開始執行，記錄狀態（不發送 running 卡片）。"""
        self.active_tools[tc_id] = {
            "tool": payload.get("tool", "unknown"),
            "emoji": payload.get("emoji", ""),
            "label": payload.get("label", payload.get("tool", "unknown")),
            "arguments": payload.get("arguments", {}),
        }
    
    def on_tool_completed(self, tc_id: str, payload: dict, 
                          completion_id: str, created: int, model: str) -> List[bytes]:
        """
        工具完成 → 只注入 <details type="tool_calls" done="true"> 到 content stream。
        這樣 Open WebUI 會正確渲染 tool card，並把 result 存進歷史訊息。
        """
        state = self.active_tools.pop(tc_id, {})
        state["result"] = payload.get("result", "")
        state["arguments"] = payload.get("arguments", state.get("arguments", {}))
        
        tool_name = state.get("tool", "unknown")
        result = state.get("result", "")
        
        chunks = []
        
        # ✅ 只注入帶 arguments + result 的 <details>（正確做法）
        emoji = state.get("emoji", get_tool_emoji(tool_name))
        label = state.get("label", tool_name)
        details = _build_completion_details(tool_name, label, result)
        
        # 加 \n\n 確保 Markdown 正確解析 <details> block
        chunks.append(_build_content_chunk(f"\n\n{details}"))
        
        return chunks
    
    @property
    def has_active_tools(self) -> bool:
        return bool(self.active_tools)


async def transform_stream(
    reader: aiohttp.StreamReader,
    model: str,
    completion_id: str,
    created: int,
    upstream_port: str,
    strip_details: bool = False,
) -> AsyncGenerator[bytes, None]:
    """
    從 Hermes 上游讀取 SSE stream，即時轉換 hermes.tool.progress 事件。

    TOOL_MODE 控制處理策略：
    - passthrough: 直接透傳所有資料
    - enhance: 過濾 done=false + 在 completed 時注入帶 label 的完成標籤
    - strip: 移除 <details> 並替換為純文字
    - enhance-v2: 推薦模式（即時串流 + 正確 tool card）
      - content 正常即時輸出
      - 只在 completed 時注入 <details type="tool_calls" done="true" arguments="..." result="...">
      - **不** emit delta.tool_calls（避免 Open WebUI 重複執行工具）
    
    性能優化：
    - 使用 bytes buffer 避免反覆 decode/encode
    - 單次 JSON 解析，緩存結果供後續使用
    - 即時輸出而非累積大字符串
    
    心跳機制：
    - 獨立於數據處理循環，每 10 秒發送一次心跳
    - 確保即使上游暫時沒有數據，客戶端也不會超時
    """

    # Track tool states for legacy modes
    tool_states: Dict[str, dict] = {}
    
    done_received = False
    split_done = False  # 是否已發送過分割標記

    # 使用 bytes buffer 避免反覆 decode/encode
    buffer = b""
    
    # 自動分割計數器
    accumulated_content = ""
    has_split = False
    
    # 心跳計時器，防止超時
    last_heartbeat = time.monotonic()
    heartbeat_interval = 10.0  # 每 10 秒發送心跳（比 gateway 的 30 秒更頻繁）
    
    # enhance-v2 專用緩衝器
    v2_buffer = ToolCallBuffer() if TOOL_MODE == "enhance-v2" else None

    while True:
        # ── 心跳檢查：在讀取數據前檢查，確保即使沒有數據也會發送心跳 ──
        now = time.monotonic()
        if now - last_heartbeat > heartbeat_interval:
            yield b": heartbeat\n\n"
            last_heartbeat = now

        line = await reader.readline()

        # Empty line means end of connection
        if not line:
            break

        buffer += line

        # Process complete SSE frames (terminated by \n\n)
        while b"\n\n" in buffer:
            frame_bytes, buffer = buffer.split(b"\n\n", 1)
            frame = frame_bytes.decode("utf-8", errors="replace")

            # 心跳檢查：處理數據時也更新心跳時間戳
            last_heartbeat = time.monotonic()

            # Check for [DONE] signal early - stop processing after it
            if "[DONE]" in frame and not done_received:
                yield (frame + "\n\n").encode("utf-8")
                done_received = True
                break

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
            
            # 單次 JSON 解析，緩存結果
            parsed_json = None
            if data_str:
                try:
                    parsed_json = json.loads(data_str)
                except json.JSONDecodeError:
                    pass

            # Handle hermes.tool.progress events
            if event_type == "hermes.tool.progress":
                if parsed_json:
                    tc_id = parsed_json.get("toolCallId", "")
                    status = parsed_json.get("status", "")
                    tool = parsed_json.get("tool", "unknown")
                    arguments = parsed_json.get("arguments", {})
                    result = parsed_json.get("result", "")

                    # ── enhance-v2 模式 ──
                    if TOOL_MODE == "enhance-v2" and v2_buffer:
                        if status == "running":
                            v2_buffer.on_tool_running(tc_id, parsed_json)
                        elif status == "completed":
                            # 立即輸出標準格式（不緩衝，直接返回 chunks）
                            chunks = v2_buffer.on_tool_completed(
                                tc_id, parsed_json, completion_id, created, model
                            )
                            for chunk in chunks:
                                yield chunk
                        # 跳過 hermes.tool.progress 事件，不發送給客戶端
                        continue
                    
                    # ── 其他模式 ──
                    if status == "running":
                        tool_states[tc_id] = {
                            "tool": tool,
                            "emoji": parsed_json.get("emoji", ""),
                            "label": parsed_json.get("label", tool),
                            "arguments": arguments if isinstance(arguments, dict) else {},
                            "result": "",
                        }
                        # 立即發送 running 狀態的佔位符，保持 stream 活躍
                        if TOOL_MODE == "enhance":
                            emoji = parsed_json.get("emoji", get_tool_emoji(tool))
                            label = parsed_json.get("label", tool)
                            yield _build_content_chunk(
                                f'<details type="tool_calls" done="false" id="{tc_id}" name="{html.escape(tool)}">\n'
                                f'<summary>{emoji} Running... {html.escape(label)}</summary>\n'
                                f'</details>\n'
                            )
                    elif status == "completed":
                        state = tool_states.pop(tc_id, {})
                        final_result = parsed_json.get("result", "")
                        
                        # enhance 模式: 注入完成標籤
                        if TOOL_MODE == "enhance":
                            tool_name = state.get("tool", tool)
                            label = state.get("label", "")
                            res = final_result if final_result else state.get("result", "")
                            yield handle_tool_completion(tool_name, label, res)
                # Do NOT yield - skip this frame
                continue

            # Handle <details> based on TOOL_MODE
            if data_str and ("<details" in data_str or "<details" in frame):
                if TOOL_MODE == "strip":
                    modified_frame = _strip_details_from_content(frame)
                elif TOOL_MODE == "enhance":
                    # 過濾掉 done="false" 的標籤（只保留 completed 時注入的 done="true"）
                    if parsed_json:
                        try:
                            delta = parsed_json.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if 'done="false"' in content:
                                continue
                        except (IndexError, KeyError):
                            pass
                    modified_frame = frame
                elif TOOL_MODE == "enhance-v2":
                    # enhance-v2: 過濾掉 Gateway 發送的原始 <details> 標籤
                    # 我們自己在 completed 時注入正確的格式
                    if parsed_json:
                        try:
                            delta = parsed_json.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if "<details" in content:
                                continue
                        except (IndexError, KeyError):
                            pass
                    modified_frame = frame
                else:
                    # passthrough: keep <details> as-is
                    modified_frame = frame
            else:
                modified_frame = frame
            
            # 自動分割檢查（使用已解析的 JSON）
            if AUTO_SPLIT_THRESHOLD > 0 and not has_split and parsed_json:
                choices = parsed_json.get("choices")
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
            
            # 即時輸出，避免累積
            yield (modified_frame + "\n\n").encode("utf-8")

        # 如果收到 [DONE]，跳出外層循環
        if done_received:
            break

    # Flush remaining buffer with proper SSE termination
    if buffer.strip():
        # Ensure the residual buffer ends with \n\n for proper SSE framing
        cleaned = buffer.decode("utf-8", errors="replace").rstrip("\r\n")
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
    resp_body = b""
    resp_status = 502
    resp_headers = {}
    try:
        async with sess.request(
            method, upstream_url, data=body, headers=fwd_headers
        ) as resp:
            resp_body = await resp.read()
            resp_status = resp.status
            resp_headers = dict(resp.headers)
            try:
                parsed = json.loads(resp_body) if resp_body else {}
            except json.JSONDecodeError:
                parsed = {}
            return JSONResponse(
                content=parsed,
                status_code=resp_status,
            )
    except json.JSONDecodeError:
        return Response(
            content=resp_body,
            status_code=resp_status,
            headers=resp_headers,
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
                logger.info(f"[port={upstream_port}] Upstream disconnected (expected after auto-split)")
            except aiohttp.ClientError as e:
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
