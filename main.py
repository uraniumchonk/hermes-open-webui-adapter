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
    level=logging.DEBUG,
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


def _build_completion_details(tool_name: str, label: str = "", result: str = "", arguments: Optional[dict] = None) -> str:
    """
    Build a complete <details> tag for a completed tool call.
    
    - 確保 name 屬性正確（不會為空）
    - <summary> 顯示工具名稱 + emoji（讓模型與用戶都能識別工具）
    - 完整 arguments JSON 放在 <arguments> 標籤內（讓模型能理解輸入參數）
    - 結果放在 <result> 標籤內（避免 HTML 實體編碼問題）
    - 結果截斷（最多 5000 字元）
    """
    safe_name = html.escape(tool_name) if tool_name else "unknown"
    
    attrs = f'type="tool_calls" done="true" name="{safe_name}"'
    
    # <summary> 包含工具名稱 + emoji，讓工具身份明確可見
    emoji = get_tool_emoji(tool_name)
    display_name = label if label else tool_name
    safe_display = html.escape(display_name)
    inner = f"\n<summary>✅ {emoji} {safe_display}</summary>"
    
    # <arguments> 標籤：優先使用完整 arguments，否則 fallback 到 label
    if arguments:
        args_str = json.dumps(arguments, ensure_ascii=False)
        inner += f"\n<arguments>{html.escape(args_str)}</arguments>"
    elif label:
        inner += f"\n<arguments>{html.escape(label)}</arguments>"
    
    if result:
        # result 放在標籤內，用 html.escape 避免 XSS
        truncated = result[:5000] + ("..." if len(result) > 5000 else "")
        inner += f"\n<result>{html.escape(truncated)}</result>"
    
    return f'<details {attrs}>{inner}\n</details>'


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
        try:
            state = self.active_tools.pop(tc_id, {})
            state["result"] = payload.get("result", "")
            state["arguments"] = payload.get("arguments", state.get("arguments", {}))
            
            tool_name = state.get("tool", "unknown")
            result = state.get("result", "")
            
            chunks = []
            
            # ✅ 只注入帶 arguments + result 的 <details>（正確做法）
            emoji = state.get("emoji", get_tool_emoji(tool_name))
            label = state.get("label", tool_name)
            arguments = state.get("arguments", {})
            details = _build_completion_details(tool_name, label, result, arguments)
            
            # 加 \n\n 確保 Markdown 正確解析 <details> block
            # 整個 <details> 在一個 chunk 中發出，避免被分割
            chunks.append(_build_content_chunk(f"\n\n{details}\n"))
            
            logging.info(f"[enhance-v2] Tool completed: {tool_name} (result_len={len(result)}, chunks={len(chunks)})")
            return chunks
        except Exception as e:
            logging.error(f"[enhance-v2] on_tool_completed ERROR: {e} for tc_id={tc_id}")
            return []
    
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
    heartbeat_interval = 1.5  # 每 1.5 秒發送心跳（比 Open WebUI idle timeout 短）
    heartbeat_count = 0  # 心跳計數器
    
    # enhance-v2 專用緩衝器
    v2_buffer = ToolCallBuffer() if TOOL_MODE == "enhance-v2" else None
    
    # 過渡期追蹤：tool completed 後的第一個 content chunk 需要特別記錄
    tool_just_completed = False
    tool_completed_at = 0  # 記錄 tool completed 的時間戳
    
    # 主循環
    while True:
        # 心跳檢查 — 在 readline 之前檢查，確保即使 upstream 沒有數據也能發送心跳
        elapsed = time.monotonic() - last_heartbeat
        if elapsed >= heartbeat_interval:
            # 雙重保險：SSE comment + empty content delta
            # 某些 client 對 comment 反應更好，某些對 data chunk 反應更好
            heartbeat_count += 1
            # 如果在 tool completed 後，記錄 idle 時間
            idle_since_tool = 0
            if tool_just_completed and tool_completed_at > 0:
                idle_since_tool = time.monotonic() - tool_completed_at
            logger.info(
                f"[enhance-v2] Heartbeat #{heartbeat_count} ({elapsed:.1f}s), "
                f"tool_just_completed={tool_just_completed}, "
                f"idle_since_tool={idle_since_tool:.1f}s, "
                f"done_received={done_received}, buffer_len={len(buffer)}"
            )
            yield b': keepalive\n\n'
            yield b'data: {"id":"%s","object":"chat.completion.chunk","created":%d,"model":"%s","choices":[{"index":0,"delta":{"content":""},"finish_reason":null}]}\n\n' % (
                completion_id.encode(), created, model.encode()
            )
            last_heartbeat = time.monotonic()
            tool_just_completed = False

        # 非阻塞讀取 — 使用 asyncio.wait_for 確保不會永久阻塞
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=1.0)
        except asyncio.TimeoutError:
            # 超時了，繼續循環，下次心跳會發送 empty delta
            continue
        except Exception as e:
            # readline() 可能丟出 exception（例如 client 斷開連線）
            logger.error(
                f"[enhance-v2] readline() exception: {type(e).__name__}: {e}, "
                f"tool_just_completed={tool_just_completed}, "
                f"done_received={done_received}, buffer_len={len(buffer)}"
            )
            raise

        # Empty line means end of connection — LOG THIS!
        if not line:
            elapsed = time.monotonic() - last_heartbeat
            logger.info(
                f"[enhance-v2] Upstream EOF detected! "
                f"last_heartbeat={elapsed:.1f}s ago, "
                f"done_received={done_received}, "
                f"buffer_len={len(buffer)}, "
                f"tool_just_completed={tool_just_completed}"
            )
            break

        buffer += line

        # Process complete SSE frames (terminated by \n\n)
        while b"\n\n" in buffer:
            frame_bytes, buffer = buffer.split(b"\n\n", 1)
            frame = frame_bytes.decode("utf-8", errors="replace")

            # 心跳檢查：處理數據時也更新心跳時間戳
            last_heartbeat = time.monotonic()

            # Check for [DONE] signal - mark it but DON'T break immediately
            # 關鍵修復：[DONE] 不代表 upstream 已經結束，agent loop 可能還在執行
            # 我們標記 done_received，但繼續讀取直到 upstream 真正關閉 (EOF)
            if "[DONE]" in frame and not done_received:
                yield (frame + "\n\n").encode("utf-8")
                done_received = True
                logger.info(
                    f"[enhance-v2] ⚠️ Received [DONE] from upstream. "
                    f"tool_just_completed={tool_just_completed}, "
                    f"heartbeat_count={heartbeat_count}, "
                    f"buffer_len={len(buffer)} — 繼續等待 upstream EOF"
                )
                # ✅ 不再 break — 繼續讀取，讓 upstream 自然關閉
                continue

            try:
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
                if event_type == "hermes.tool.progress" and parsed_json:
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
                            # Tool completed 後立即發送多個 nudge（不阻塞）
                            # 確保 Open WebUI 的 idle timer 被重置
                            for i in range(3):
                                yield b': keepalive-post-tool\n\n'
                                yield b'data: {"id":"%s","object":"chat.completion.chunk","created":%d,"model":"%s","choices":[{"index":0,"delta":{"content":""},"finish_reason":null}]}\n\n' % (
                                    completion_id.encode(), created, model.encode()
                                )
                            # 發送可見的 thinking chunk，讓 Open WebUI 知道還在處理
                            yield _build_content_chunk("\n\n")
                            logger.info(
                                f"[enhance-v2] Tool '{tool}' completed (tc_id={tc_id[:20]}...), "
                                f"sent 3 nudges + thinking chunk to keep stream alive"
                            )
                            tool_just_completed = True
                            tool_completed_at = time.monotonic()  # 記錄 tool completed 時間
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
                        # enhance-v2: 只過濾 Gateway 發送的 <details type="tool_calls"> 標籤
                        # 避免誤殺正常內容中包含 <details 字串的情況
                        if parsed_json:
                            try:
                                delta = parsed_json.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content", "")
                                # 只過濾我們自己的 tool_calls details 標籤
                                if 'type="tool_calls"' in content or 'type="tool_calls">' in content:
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
                # 過渡期 logging：tool completed 後的第一個 content chunk
                if tool_just_completed and modified_frame:
                    try:
                        fc = json.loads(modified_frame) if not modified_frame.startswith(':') else None
                        if fc:
                            delta = fc.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                logger.info(
                                    f"[enhance-v2] Post-tool transition: first content chunk "
                                    f"({len(content)} chars) -> {content[:80]}..."
                                )
                                tool_just_completed = False
                    except (json.JSONDecodeError, IndexError, KeyError):
                        tool_just_completed = False
                yield (modified_frame + "\n\n").encode("utf-8")
                
            except Exception as e:
                logging.error(f"[transform_stream] Frame processing ERROR: {e} | frame_preview={frame[:200]}")
                continue

        # ✅ 關鍵修復：收到 [DONE] 後不再立即跳出
        # 而是繼續等待 upstream 真正關閉 (EOF)
        # 這樣可以避免 prematurely 關閉與 Gateway 的連線，
        # 導致 Gateway 認為 client disconnected → interrupt agent loop

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
                # Client (Open WebUI) disconnected — gracefully close upstream
                logger.info(f"[port={upstream_port}] Client disconnected, closing upstream gracefully")
                if upstream_resp is not None:
                    upstream_resp.close()
            except aiohttp.ServerDisconnectedError:
                # Upstream disconnected after auto-split — this is expected, not an error
                logger.info(f"[port={upstream_port}] Upstream disconnected (expected after auto-split)")
            except aiohttp.ClientError as e:
                # Other client errors (connection reset, timeout, etc.)
                logger.warning(f"[port={upstream_port}] Client error: {e}")
            except Exception as e:
                # CWE-209/CWE-497: 不要在 client 回應中洩露內部錯誤細節
                logger.error(f"[port={upstream_port}] Proxy error: {type(e).__name__}: {e}", exc_info=True)
                yield b'data: {"error":{"message":"Internal proxy error","type":"proxy_error","code":"upstream_failure"}}\n\n'
            finally:
                # Ensure upstream response is closed even on unexpected exits
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
                # CWE-209/CWE-497: 不要在 client 回應中洩露內部錯誤細節
                logger.error(f"[port={upstream_port}] Proxy error: {type(e).__name__}: {e}", exc_info=True)
                yield b'data: {"error":{"message":"Internal proxy error","type":"proxy_error","code":"upstream_failure"}}\n\n'

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
