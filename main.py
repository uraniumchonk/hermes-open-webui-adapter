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
import random
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, AsyncGenerator, List

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response, JSONResponse

# ── Handler modules ────────────────────────────────────────
from completions_handler import handle_completions_request
from responses_handler import handle_responses_request
import tool_history_format
from comp_mode import compress_tool_results
import native_tool_context
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# ── Logging ───────────────────────────────────────────────
# Production default INFO. Set TOOL_FILTER_LOG_LEVEL=DEBUG for deep tracing.
# DEBUG on a long-lived SSE proxy can retain huge format args and flood journald.
_LOG_LEVEL = getattr(logging, os.environ.get("TOOL_FILTER_LOG_LEVEL", "INFO").upper(), logging.INFO)

# ── 雙重日誌：console + file ──
# 將關鍵錯誤和除錯資訊寫入 .log 文件，不依賴 systemd journal
LOG_FILE = Path(__file__).parent / "hermes_tool_filter.log"

# Console handler (systemd journal)
console_handler = logging.StreamHandler()
console_handler.setLevel(_LOG_LEVEL)
console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

# File handler (persistent log for debugging)
file_handler = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)  # 文件記錄所有 DEBUG 級別
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s"))

# Root logger
logging.basicConfig(
    level=logging.DEBUG,
    handlers=[console_handler, file_handler],
)
logger = logging.getLogger("tool-filter")
logger.setLevel(logging.DEBUG)

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
# 優先使用 config.yaml 的 upstreams，fallback 到硬編碼預設值
_DEFAULT_PORT_MAP: Dict[str, str] = {
    "30000": "http://127.0.0.1:30000",
    "30001": "http://127.0.0.1:30001",
    "30002": "http://127.0.0.1:30002",
    "30003": "http://127.0.0.1:30003",
}

# 從 config.yaml 載入 upstreams（如果有的話）
_config_upstreams = CONFIG.get("upstreams", {})
if _config_upstreams:
    PORT_MAP: Dict[str, str] = dict(_DEFAULT_PORT_MAP)
    PORT_MAP.update(_config_upstreams)
    logger.info(f"PORT_MAP loaded from config.yaml: {PORT_MAP}")
else:
    PORT_MAP = _DEFAULT_PORT_MAP

# Default upstream if no port prefix matched
DEFAULT_UPSTREAM = PORT_MAP.get("30000", "http://127.0.0.1:30000")

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


def sanitize_request_messages(
    messages: list, model: str = "", hermes_sid: str = ""
) -> list:
    """
    Scan and sanitize all messages in the request to prevent <details> pollution.
    Only processes assistant role content.

    Always uses structured format (OpenAI native tool role messages).
    """
    if not messages:
        return messages

    enabled = tool_history_format._get_sanitization_config(CONFIG)[0]
    if not enabled:
        return messages

    from tool_history_structured import sanitize_messages_structured
    return sanitize_messages_structured(messages, CONFIG)


# ── Client-Side [comp] Compression ─────────────────────────
#
# When the user includes [comp] in their message, compress all tool results
# in the conversation history BEFORE that message to reduce context window size.
#
# This is a client-side compression that directly modifies the messages array
# sent to the model — unlike server-side compression which relies on Gateway's
# state.db. Useful when you want to manually control context size mid-conversation.
#
# Modes:
#   enabled   — Active. Scans for [comp] in the last user message, then truncates
#               all tool results in previous messages to a summary + inserts a
#               marker so the LLM knows history was compressed.
#   disabled  — No client-side compression (default).
#
# If the user sends ONLY "[comp]" (no other content), the proxy returns a
# pre-written auto-reply directly without forwarding to the LLM. The compressed
# messages + marker are still processed, so the conversation context includes
# the compression notification.

_COMP_TRIGGER = "[comp]"

_COMP_MARKER_CODE = "comp"

_COMP_NOTIFICATION = """[CONVERSATION COMPRESSED] Previous tool results have been truncated to save context space. The full results are still available in the server-side session history. If you need to reference specific data from earlier tool calls, please re-run the relevant tools to reload the data into context."""

# Auto-reply text when user sends ONLY "[comp]" — returned directly without LLM.
_COMP_AUTO_REPLY = """[CONVERSATION COMPRESSED] Tool execution history has been truncated to reduce context size. You can now continue with new instructions."""


def _comp_mode_enabled(config: dict = None) -> bool:
    """Check if client-side compression mode is enabled."""
    cfg = config if config is not None else CONFIG
    return cfg.get("comp_mode", "disabled") == "enabled"


def _strip_comp_trigger(content):
    """Remove [comp] trigger from user message content."""
    if isinstance(content, str):
        return content.replace(_COMP_TRIGGER, "").strip()
    elif isinstance(content, list):
        new_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                new_parts.append({**part, "text": part["text"].replace(_COMP_TRIGGER, "").strip()})
            else:
                new_parts.append(part)
        return new_parts
    return content


def _compress_prev_action_blocks(content: str, max_length: int) -> tuple[str, int]:
    """
    Compress [START_PREV_ACTION]...[END_PREV_ACTION] blocks in content.
    
    **只壓縮最後一個區塊** — 保留歷史上下文，只截斷最新的工具結果。
    Keeps the tool name and args, but truncates the [RESULT] section.
    Returns (compressed_content, blocks_compressed).
    """
    if not content:
        return (content, 0)
    
    # 找到所有區塊的位置
    pattern = r'\[START_PREV_ACTION\](.*?)\[END_PREV_ACTION\]'
    matches = list(re.finditer(pattern, content, flags=re.DOTALL))
    
    if not matches:
        return (content, 0)
    
    # 只壓縮最後一個區塊
    last_match = matches[-1]
    blocks_compressed = 1
    
    block_inner = last_match.group(1)
    
    # Extract tool name
    tool_name_match = re.search(r'\[ACTION_TYPE\]\s*\n\s*([^\n]+)', block_inner)
    tool_name = tool_name_match.group(1).strip() if tool_name_match else "unknown"
    
    # Extract args section
    args_match = re.search(r'\[ACTION_ARG\]\s*\n(.*?)(?=\n\[RESULT\]|\n\[END)', block_inner, re.DOTALL)
    args_text = args_match.group(1).strip() if args_match else "(none)"
    if len(args_text) > 100:
        args_text = args_text[:100] + "..."
    
    # Build compressed block
    if max_length <= 0:
        result_text = "(compressed)"
    else:
        result_text = f"(compressed from original, {max_length} chars kept)"
    
    compressed_block = (
        f"[START_PREV_ACTION]\n"
        f"[ACTION_TYPE]\n"
        f"{tool_name}\n"
        f"[ACTION_ARG]\n"
        f"{args_text}\n"
        f"[RESULT]\n"
        f"{result_text}\n"
        f"[END_PREV_ACTION]"
    )
    
    # 只替換最後一個區塊
    compressed = content[:last_match.start()] + compressed_block + content[last_match.end():]
    
    # 除錯: 記錄壓縮前後的大小
    original_size = last_match.end() - last_match.start()
    new_size = len(compressed_block)
    logger.debug(f"[comp] PREV_ACTION block: {original_size} -> {new_size} chars ({(1-new_size/original_size)*100:.1f}% reduction)")
    
    return (compressed, blocks_compressed)


def _compress_details_tags(content: str, max_length: int) -> tuple[str, int]:
    """
    Compress <details type="tool_calls"> tags in content.
    
    **只壓縮最後一個標籤** — 保留歷史上下文，只截斷最新的工具結果。
    Replaces the <result> section with a truncated version.
    Returns (compressed_content, tags_compressed).
    """
    if not content:
        return (content, 0)
    
    # 修正: 支援 type="tool_calls" 和 type=tool_calls (有/無引號)
    pattern = r'(<details[^>]*type=["\']?tool_calls["\']?[^>]*>)(.*?)(</details>)'
    
    # 找到所有標籤的位置
    matches = list(re.finditer(pattern, content, flags=re.DOTALL | re.IGNORECASE))
    
    if not matches:
        return (content, 0)
    
    # 只壓縮最後一個標籤
    last_match = matches[-1]
    tags_compressed = 1
    
    opening = last_match.group(1)
    inner = last_match.group(2)
    closing = last_match.group(3)
    
    # Find and compress <result> section
    result_pattern = r'(<result>)(.*?)(</result>)'
    def _compress_result(rm):
        result_content = rm.group(2)
        if len(result_content) > max_length:
            return f"{rm.group(1)}{result_content[:max_length]}... (truncated by [comp]){rm.group(3)}"
        return rm.group(0)
    
    inner_compressed = re.sub(result_pattern, _compress_result, inner, flags=re.DOTALL)
    compressed_tag = f"{opening}{inner_compressed}{closing}"
    
    # 只替換最後一個標籤
    compressed = content[:last_match.start()] + compressed_tag + content[last_match.end():]
    return (compressed, tags_compressed)


def compress_tool_results(messages: list, config: dict) -> tuple[list, bool]:
    """
    Client-side conversation compression triggered by [comp] in user message.
    
    When enabled and [comp] is detected in the last user message:
    1. Remove [comp] from the user message
    2. Compress all tool results in messages BEFORE this user message
    3. Insert a notification message so the LLM knows history was compressed
    4. Insert a persistent marker code block
    
    Returns (modified_messages, is_comp_only).
    - is_comp_only=True means the user sent ONLY "[comp]" — caller should
      return an auto-reply directly without forwarding to the LLM.
    - is_comp_only=False means normal compression (still forward to LLM).
    """
    if not _comp_mode_enabled(config):
        return (messages, False)
    
    if not messages:
        return (messages, False)
    
    max_length = config.get("comp_result_max_length", 100)
    
    # Find the last user message and check for [comp]
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break
    
    if last_user_idx is None:
        return (messages, False)
    
    last_user = messages[last_user_idx]
    content = last_user.get("content", "")
    
    # Check if [comp] is present AND if it's the ONLY content
    has_comp = False
    is_comp_only = False
    if isinstance(content, str):
        has_comp = _COMP_TRIGGER in content
        # is_comp_only: content is exactly "[comp]" after stripping whitespace
        is_comp_only = content.strip() == _COMP_TRIGGER
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and _COMP_TRIGGER in part.get("text", ""):
                has_comp = True
                # is_comp_only: single text part that is exactly "[comp]"
                if len(content) == 1 and part.get("text", "").strip() == _COMP_TRIGGER:
                    is_comp_only = True
                break
    
    if not has_comp:
        return (messages, False)
    
    # ── [comp] detected — perform compression ──
    
    # 1. Strip [comp] from the user message
    last_user["content"] = _strip_comp_trigger(last_user["content"])
    
    # 2. Compress tool results in all messages BEFORE this user message
    total_compressed = 0
    
    # 除錯: 記錄壓縮前的總大小
    original_total_size = sum(len(str(m.get("content", ""))) for m in messages[:last_user_idx])
    
    for i in range(last_user_idx):
        msg = messages[i]
        role = msg.get("role", "")
        raw_content = msg.get("content", "")
        
        if not raw_content:
            continue
        
        # Handle both string and list content
        if isinstance(raw_content, str):
            # Compress [START_PREV_ACTION] blocks (flat format)
            compressed, count1 = _compress_prev_action_blocks(raw_content, max_length)
            # Also compress <details> tags if present
            compressed, count2 = _compress_details_tags(compressed, max_length)
            msg["content"] = compressed
            total_compressed += count1 + count2
            
        elif isinstance(raw_content, list):
            for part in raw_content:
                if isinstance(part, dict) and part.get("type") == "text":
                    compressed, count1 = _compress_prev_action_blocks(part["text"], max_length)
                    compressed, count2 = _compress_details_tags(compressed, max_length)
                    part["text"] = compressed
                    total_compressed += count1 + count2
    
    # 除錯: 記錄壓縮後的總大小
    new_total_size = sum(len(str(m.get("content", ""))) for m in messages[:last_user_idx])
    logger.info(
        f"[comp] Total size: {original_total_size} -> {new_total_size} chars "
        f"({(1-new_total_size/original_total_size)*100:.1f}% reduction, {total_compressed} blocks)"
    )
    
    # 3. Insert compression marker as a system message right before the last user message
    marker_msg = {
        "role": "system",
        "content": f"{_COMP_NOTIFICATION}\n\n```\n{_COMP_MARKER_CODE}\ncompression: applied at message {last_user_idx}\nblocks_compressed: {total_compressed}\nmax_result_length: {max_length}\n```",
    }
    messages.insert(last_user_idx, marker_msg)
    
    logger.info(
        f"[comp] Compression applied: {total_compressed} tool result(s) compressed "
        f"across {last_user_idx} message(s), marker inserted at index {last_user_idx}"
    )
    
    return (messages, is_comp_only)


# ── Conversation Compression ───────────────────────────────
#
# 當 X-Hermes-Session-Id 存在時，Hermes Gateway 會從 state.db 載入完整
# 會話歷史，忽略請求中的 messages 歷史。因此我們可以只傳送 system prompt
# + 最後一則 user message，大幅減少請求大小。
#
# 配置：config.yaml 中的 compression_mode
#   server-side — 依賴 Gateway 的 server-side history（預設）
#   disabled    — 傳送完整 messages（舊版行為）


# ── Session Isolation (Collision Prevention) ────────────────
#
# When two conversations start with identical first messages, they would
# collide on the same session ID. This feature prevents that by:
# 1. Injecting a unique timestamp into the first user message
# 2. Embedding a session marker in the assistant's response
# 3. On subsequent requests, recovering the session from the marker
#
# Configuration: config.yaml session_isolation_mode
#   disabled — No isolation (default, relies on X-Hermes-Session-Id header)
#   marker   — Full isolation with visible code block markers


import hashlib


# Global session cache: {original_fingerprint → hermes_session_id}
_session_cache: Dict[str, str] = {}

# Pending session markers: {stamped_session_id: (original_fp, timestamp)}
# Use a dict instead of a single global to avoid race conditions between requests.
_pending_session_markers: dict = {}

# 防洩漏：session 指紋快取長期運行會無限增長，加上限並丟棄最舊的。
_MAX_SESSION_CACHE = 5000
_MAX_PENDING_MARKERS = 1000


def _bounded_put(cache: dict, key: str, value, max_size: int) -> None:
    """有序 dict 的 bounded insert：超過上限時丟棄最舊的 10%。"""
    cache[key] = value
    if len(cache) > max_size:
        drop = list(cache.keys())[: max(1, len(cache) // 10)]
        for k in drop:
            cache.pop(k, None)


def _session_isolation_enabled() -> bool:
    """Check if session isolation (marker mode) is enabled."""
    return CONFIG.get("session_isolation_mode", "disabled") == "marker"


def derive_session_id(messages: list) -> str:
    """
    Derive a stable session ID from the conversation's first user message.

    Matches API Server's _derive_chat_session_id():
    seed = f"{system_prompt}\\n{first_user_message}"
    digest = sha256(seed).hexdigest()[:16]
    return f"api-{digest}"
    """
    if not messages:
        return ""

    # Extract system prompt
    system_prompt = ""
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str):
                system_prompt = content
            elif isinstance(content, list):
                system_prompt = "".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            break

    # Extract first user message
    first_user = ""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                first_user = content
            elif isinstance(content, list):
                first_user = "".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            break

    seed = f"{system_prompt}\n{first_user}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"api-{digest}"


_TS_RE = re.compile(r"\n```session\s*\n.*?\n```\n", re.DOTALL)


def _strip_timestamp_and_derive(messages: list) -> str:
    """Strip the injected timestamp from the first user message and re-derive."""
    if not messages:
        return ""

    system_prompt = ""
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str):
                system_prompt = content
            elif isinstance(content, list):
                system_prompt = "".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            break

    first_user = ""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                first_user = _TS_RE.sub("", content)
            elif isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append(_TS_RE.sub("", p.get("text", "")))
                    else:
                        parts.append(p.get("text", ""))
                first_user = "".join(parts)
            break

    seed = f"{system_prompt}\n{first_user}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"api-{digest}"


def get_or_create_session_id(messages: list) -> str:
    """
    Get existing session ID from cache, or derive a new one with collision prevention.

    Strategy to prevent collisions when two conversations start with identical
    content:

    1. First, scan the conversation history for an embedded session marker in
       any assistant message (code block or legacy zero-width space format).
       If found, we reuse that session.

    2. If no marker is found, this is a new conversation. We inject a timestamp
       into the first user message so the Gateway creates a unique session,
       and arrange for the marker to be embedded in the assistant's reply.

    3. On the very next request, step-1 picks up the marker from history.

    Cache structure: {original_fingerprint → hermes_session_id}
    """
    from datetime import datetime

    # ── Step 1: Look for an embedded session marker in assistant history ──
    marker_pattern = re.compile(
        r"```session\s*\n\s*(api-[a-f0-9]{16})\s+(\d{4}-\d{2}-\d{2}T[^\s]+)\s*\n```",
        re.IGNORECASE
    )
    legacy_pattern = re.compile(
        r"(api-[a-f0-9]{16}):(\d{4}-\d{2}-\d{2}T[^\s\u200b]+)", re.IGNORECASE
    )
    found_ts = None
    for msg in reversed(messages):
        role = msg.get("role")
        if role == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str):
                m = marker_pattern.search(content)
                if m:
                    found_ts = m.group(1), m.group(2)
                    logger.info(f"[session] ✅ Marker found in assistant message (code block): {found_ts[0][:8]}...")
                    break
                m = legacy_pattern.search(content)
                if m:
                    found_ts = m.group(1), m.group(2)
                    logger.info(f"[session] ✅ Marker found in assistant message (legacy): {found_ts[0][:8]}...")
                    break
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        m = marker_pattern.search(part["text"])
                        if m:
                            found_ts = m.group(1), m.group(2)
                            logger.info(f"[session] ✅ Marker found in assistant message (list, code block): {found_ts[0][:8]}...")
                            break
                        m = legacy_pattern.search(part["text"])
                        if m:
                            found_ts = m.group(1), m.group(2)
                            logger.info(f"[session] ✅ Marker found in assistant message (list, legacy): {found_ts[0][:8]}...")
                            break
        if found_ts:
            break

    if not found_ts:
        logger.info(f"[session] ⚠️ No marker found in {len(messages)} messages")

    if found_ts:
        original_fp, recovered_ts = found_ts
        if original_fp in _session_cache:
            logger.info(f"[session] ✅ Marker found in history: {original_fp[:8]}... → cache hit")
            return _session_cache[original_fp]
        logger.warning(f"[session] Marker found but NOT in cache: {original_fp[:8]}...")
        # Fallback: reconstruct stamped fingerprint and try that
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                ts_marker = f"\n```session\n{original_fp}  {recovered_ts}\n```\n"
                if isinstance(content, str):
                    msg["content"] = ts_marker + content
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            part["text"] = ts_marker + part["text"]
                            break
                break
        stamped_fp = derive_session_id(messages)
        if stamped_fp in _session_cache:
            return _session_cache[stamped_fp]
        return original_fp

    # ── Step 2: No marker — new conversation, inject timestamp ──
    derived = derive_session_id(messages)
    if not derived:
        return ""

    timestamp = datetime.now().isoformat()
    stamped_derived_temp = derived
    ts_marker = f"\n```session\n{stamped_derived_temp}  {timestamp}\n```\n"
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                msg["content"] = ts_marker + content
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        part["text"] = ts_marker + part["text"]
                        break
            break

    stamped_derived = derive_session_id(messages)

    # Store in cache keyed by original fingerprint
    original = _strip_timestamp_and_derive(messages)
    _bounded_put(_session_cache, original, stamped_derived, _MAX_SESSION_CACHE)

    # Remember the marker so transform_stream can embed it in the first
    # assistant response.
    _bounded_put(_pending_session_markers, stamped_derived, (original, timestamp), _MAX_PENDING_MARKERS)
    logger.info(f"[session] NEW session created: {original} → {stamped_derived}, marker pending")

    return stamped_derived


def update_session_id(messages: list, new_sid: str) -> None:
    """
    Update the cached session ID after compression rotates it.

    Hermes Gateway creates a new session after compression and returns
    the new session ID in the response header. We need to track this.

    The messages passed here may carry the injected timestamp, so we
    strip it first to find the correct cache entry.
    """
    original = _strip_timestamp_and_derive(messages)
    if original and new_sid:
        _bounded_put(_session_cache, original, new_sid, _MAX_SESSION_CACHE)


def compress_request_messages(messages: list, hermes_sid: str, config: dict) -> list:
    """
    Compress the messages array when server-side session history is available.

    When X-Hermes-Session-Id is present, Hermes Gateway loads the full
    conversation from its database. The messages in the request body are
    redundant — we only need the system prompt and the last user message.

    Returns the compressed messages list.
    """
    if not messages:
        return messages

    mode = config.get("compression_mode", "server-side")
    if mode != "server-side" or not hermes_sid:
        return messages

    if len(messages) <= 2:
        return messages

    system_msgs = [m for m in messages if m.get("role") == "system"]

    # Find the last user message
    last_user = None
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = m
            break

    if not last_user:
        return messages

    original_count = len(messages)
    original_size = sum(len(str(m.get("content", ""))) for m in messages)

    compressed = system_msgs + [last_user]
    compressed_size = sum(len(str(m.get("content", ""))) for m in compressed)

    logger.info(
        f"[compression] Reduced {original_count} messages ({original_size} chars) "
        f"→ {len(compressed)} messages ({compressed_size} chars) "
        f"via server-side session history (session={hermes_sid[:8]}...)"
    )

    return compressed


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
    - <summary> 顯示工具名稱 + emoji（供用戶視覺識別）
    - <arguments> 包含 tool_name + 完整參數（讓模型能識別工具與輸入）
    - 結果放在 <result> 標籤內（避免 HTML 實體編碼問題）
    - 結果截斷（最多 5000 字元）
    - **多模態處理**：基於 arguments 中的圖片欄位判斷，替換 base64 為簡短提示
    """
    safe_name = html.escape(tool_name) if tool_name else "unknown"
    
    # ── 確保 arguments 是 dict（hermes.tool.progress 可能傳入 JSON 字串）─
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, ValueError):
            arguments = None
    elif not isinstance(arguments, dict):
        arguments = None
    
    attrs = f'type="tool_calls" done="true" name="{safe_name}"'
    
    # <summary> 內容留空以節省上下文（arguments 已包含完整資訊）
    inner = "\n<summary></summary>"
    
    # <arguments> 標籤：包含 tool_name + 完整參數（讓模型能識別工具）
    if arguments:
        # 將 tool_name 加入 arguments，讓模型知道這是哪個工具
        full_args = {"tool_name": tool_name, **arguments}
        args_str = json.dumps(full_args, ensure_ascii=False)
        inner += f"\n<arguments>{html.escape(args_str)}</arguments>"
    elif label:
        # fallback: 只有 label，也加入 tool_name
        full_args = {"tool_name": tool_name, "label": label}
        args_str = json.dumps(full_args, ensure_ascii=False)
        inner += f"\n<arguments>{html.escape(args_str)}</arguments>"
    else:
        # 最後 fallback: 只有 tool_name
        full_args = {"tool_name": tool_name}
        args_str = json.dumps(full_args, ensure_ascii=False)
        inner += f"\n<arguments>{html.escape(args_str)}</arguments>"
    
    if result:
        # ── 多模態處理：基於 arguments 判斷是否為視覺工具 ──
        #
        # 優雅策略：檢查 arguments 中是否包含圖片相關欄位（image_url, image_path 等），
        # 而非硬編碼工具名稱清單。這樣未來新增視覺工具時不需要修改此處。
        #
        # 注意：result 可能是 dict（直接從 hermes.tool.progress 事件來）
        # 或 str（已序列化的 JSON）。兩種都需處理。
        
        # 第一步：將 dict 轉為 str，並計算原始大小
        result_str = ""  # 初始化，避免 Pyright 未綁定警告
        result_len = 0
        
        if isinstance(result, dict):
            # dict 格式：序列化後計算大小
            try:
                result_str = json.dumps(result, ensure_ascii=False)
            except (TypeError, ValueError):
                result_str = str(result)
            result_len = len(result_str)
        else:
            # str 格式：直接使用
            result_str = str(result) if not isinstance(result, str) else result
            result_len = len(result_str)
        
        # 圖片相關欄位清單（包含單數/複數形式）
        image_keys = {"image_url", "image_urls", "image_path", "image_paths", 
                      "path", "paths", "screenshot_path", "screenshot_paths",
                      "images", "image"}
        
        # 判斷：arguments 包含圖片欄位 + result 超過 10KB → 視為多模態信封包
        has_image_arg = arguments is not None and image_keys & set(arguments.keys())
        is_large_result = result_len > 10240
        
        # DEBUG: 記錄 arguments 狀態以便排查
        if is_large_result and not has_image_arg:
            logger.warning(
                f"[multimodal-debug] Large result ({result_len} bytes) but has_image_arg=False! "
                f"arguments type={type(arguments).__name__}, keys={list(arguments.keys()) if isinstance(arguments, dict) else arguments}, "
                f"tool_name={tool_name}"
            )
        elif is_large_result and has_image_arg:
            logger.info(
                f"[multimodal-debug] Large result ({result_len} bytes) detected as multimodal! "
                f"arguments keys={list(arguments.keys()) if isinstance(arguments, dict) else arguments}, "
                f"tool_name={tool_name}"
            )
        
        if has_image_arg and is_large_result:
            # 視覺工具的大結果：直接替換成簡短提示
            # 模型已經在當輪"看到"圖片了，下一輪不需要再塞幾MB的base64
            inner += f"\n<result>圖片已從上下文移除（原始大小 {result_len/1024:.1f}KB）。想要再看圖片請再調用一次視覺工具即可。</result>"
        else:
            # 一般結果：用 html.escape 避免 XSS
            truncated = result_str[:5000] + ("..." if result_len > 5000 else "")
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


def _build_comp_auto_reply_stream(
    text: str, model: str, completion_id: str, created: int
) -> StreamingResponse:
    """
    Build a streaming response for [comp] auto-reply.
    Emits the text as a series of content chunks followed by a finish chunk.
    """
    async def generate():
        # Split text into small chunks for realistic streaming feel
        chunk_size = 20
        for i in range(0, len(text), chunk_size):
            segment = text[i:i + chunk_size]
            yield _build_content_chunk(segment)
            await asyncio.sleep(0.01)  # Small delay for streaming effect
        yield _build_finish_chunk(
            completion_id, created, model,
            finish_reason="stop",
            usage={"prompt_tokens": 0, "completion_tokens": len(text), "total_tokens": len(text)}
        )

    return StreamingResponse(
        generate(),
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
        # 防洩漏：gateway 發了 running 但沒發 completed（agent 被打斷/上游斷線）時，
        # entry 會永久留在 dict。上限 + TTL 確保長期運行不會無限增長。
        self._max_tools = 500
        self._ttl_seconds = 600  # 10 分鐘沒完成就當作孤兒清理

    def _prune(self) -> None:
        """清理過期 entry；超上限時丟棄最舊的。"""
        now = time.monotonic()
        stale = [tid for tid, st in self.active_tools.items()
                 if now - st.get("_started", now) > self._ttl_seconds]
        for tid in stale:
            self.active_tools.pop(tid, None)
        while len(self.active_tools) >= self._max_tools:
            # dict 保持插入順序，pop 最舊的（在插入前執行，確保永不超過上限）
            self.active_tools.pop(next(iter(self.active_tools)), None)

    def on_tool_running(self, tc_id: str, payload: dict) -> None:
        """工具開始執行，記錄狀態（不發送 running 卡片）。"""
        self._prune()
        self.active_tools[tc_id] = {
            "tool": tool_name,
            "emoji": emoji,
            "label": payload.get("label", tool_name),
            "arguments": payload.get("arguments", {}),
            "_started": time.monotonic(),
        }
        
        # ✅ 發送空 chunk 保持 SSE stream 活躍（防止 Open WebUI idle timeout）
        chunks = []
        chunks.append(b'data: {"id":"%s","object":"chat.completion.chunk","created":%d,"model":"%s","choices":[{"index":0,"delta":{"content":""},"finish_reason":null}]}  \n\n' % (
            completion_id.encode(), created, model.encode()
        ))
        logging.info(f"[enhance-v2] Tool running: {tool_name} (sent keepalive chunk)")
        return chunks
    
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
    hermes_sid: str = "",
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
    # Hard cap: if upstream framing breaks (no \n\n), buffer must not grow without bound.
    # 16MB is far above any legitimate single SSE frame (session_search ~235KB historically).
    MAX_SSE_BUFFER = 16 * 1024 * 1024
    # Soft RSS guard — log when process RSS exceeds this (does not kill; systemd MemoryMax does).
    _rss_warn_bytes = 512 * 1024 * 1024
    _last_rss_check = 0.0

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
    
    # ✅ 修復：追蹤是否已發送第一個有內容的 chunk，避免心跳干擾
    first_content_sent = False

    def _maybe_log_rss() -> None:
        nonlocal _last_rss_check
        now = time.monotonic()
        if now - _last_rss_check < 30.0:
            return
        _last_rss_check = now
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        # VmRSS is in kB
                        rss_kb = int(line.split()[1])
                        rss_bytes = rss_kb * 1024
                        if rss_bytes >= _rss_warn_bytes:
                            logger.warning(
                                f"[mem] high RSS={rss_kb} kB buffer_len={len(buffer)} "
                                f"active_tools={len(v2_buffer.active_tools) if v2_buffer else 0} "
                                f"heartbeat_count={heartbeat_count}"
                            )
                        else:
                            logger.debug(f"[mem] RSS={rss_kb} kB buffer_len={len(buffer)}")
                        break
        except Exception:
            pass
    
    # ✅ 防火牆優化：在開始讀取 upstream 前，先發送初始心跳強制連接建立
    # 學校防火牆/代理可能會緩衝小數據包，我們用多層策略確保連接不被卡住
    
    # 策略 1: SSE comment 強制連接建立（最小包，立即刷出）
    yield b': initial-connection-established\n\n'
    
    # 策略 2: 發送初始 chunk（帶 completion_id 讓客戶端識別串流）
    yield b'data: {"id":"%s","object":"chat.completion.chunk","created":%d,"model":"%s","choices":[{"index":0,"delta":{},"finish_reason":null}]}%s\n\n' % (
        completion_id.encode(), created, model.encode(), b''
    )
    
    logger.info(f"[firewall-optimization] Sent initial packets to force connection establishment")
    
    # ✅ 新增：在等待 upstream 第一塊內容時，使用更短的心跳間隔（0.5 秒）
    # 學校網路可能需要更頻繁的心跳來保持連接活躍
    initial_wait_heartbeat = time.monotonic()
    initial_wait_interval = 0.5  # 初始等待階段每 0.5 秒發送心跳
    
    # ✅ 預建心跳 chunk 模板（避免每次重複格式化）
    # 關鍵修復：使用 data: 行而非 SSE comment，確保 Open WebUI 識別為活躍信號
    # SSE comment (:) 不被某些客戶端計入 idle timer，導致斷線
    _heartbeat_chunk_tpl = b'data: {"id":"%s","object":"chat.completion.chunk","created":%d,"model":"%s","choices":[{"index":0,"delta":{"content":""},"finish_reason":null}]}  \n\n'
    
    # 主循環
    while True:
        # ✅ 防火牆優化：在等待第一塊內容時使用更短的心跳間隔
        current_heartbeat_interval = initial_wait_interval if not first_content_sent else heartbeat_interval
        
        # 心跳檢查
        elapsed = time.monotonic() - last_heartbeat
        if elapsed >= current_heartbeat_interval:
            heartbeat_count += 1
            last_heartbeat = time.monotonic()
            tool_just_completed = False
            
            # ✅ 關鍵修復：使用 data: 行而非 SSE comment，確保 Open WebUI 識別為活躍信號
            yield _heartbeat_chunk_tpl % (
                completion_id.encode(), created, model.encode()
            )
            
            if not first_content_sent:
                logger.debug(f"[firewall-optimization] Sent keepalive while waiting for first chunk (count={heartbeat_count})")

        # 非阻塞讀取 — 使用 asyncio.wait_for 確保不會永久阻塞
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=1.0)
        except asyncio.TimeoutError:
            # 超時了，繼續循環，下次心跳會發送
            # DEBUG: 記錄 buffer 狀態，排查 gateway 是否發送數據
            if not first_content_sent and len(buffer) > 0:
                logger.warning(
                    f"[readline-debug] TIMEOUT but buffer_len={len(buffer)}, "
                    f"buffer_preview={buffer[:200]!r}, first_content_sent={first_content_sent}"
                )
            continue
        except Exception as e:
            # readline() 可能丟出 exception（例如 client 斷開連線）
            logger.error(
                f"[enhance-v2] readline() exception: {type(e).__name__}: {e}, "
                f"tool_just_completed={tool_just_completed}, "
                f"done_received={done_received}, buffer_len={len(buffer)}"
            )
            raise

        # DEBUG: 記錄所有讀取的原始行（前 100 行）
        if heartbeat_count < 100:
            line_preview = line[:100] if line else b""
            logger.debug(
                f"[readline-debug] READ line_len={len(line)}, preview={line_preview!r}, "
                f"buffer_len={len(buffer)}, first_content={first_content_sent}"
            )

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
        if len(buffer) > MAX_SSE_BUFFER:
            logger.error(
                f"[enhance-v2] SSE buffer exceeded {MAX_SSE_BUFFER} bytes "
                f"(len={len(buffer)}); aborting stream to prevent host OOM. "
                f"Likely broken upstream framing (missing \\n\\n)."
            )
            yield (
                b'data: {"error":{"message":"SSE buffer overflow in tool filter",'
                b'"type":"proxy_error","code":"sse_buffer_overflow"}}\n\n'
            )
            break

        _maybe_log_rss()

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
                # ✅ Session Isolation: inject marker before [DONE] if pending
                if _session_isolation_enabled() and hermes_sid in _pending_session_markers:
                    original_fp, ts = _pending_session_markers.pop(hermes_sid)
                    marker = f"\n```session\n{original_fp}  {ts}\n```\n"
                    marker_json = json.dumps(marker)
                    marker_chunk = (
                        f'data: {{"id":"{completion_id}","object":"chat.completion.chunk",'
                        f'"created":{created},"model":"{model}",'
                        f'"choices":[{{"index":0,"delta":{{"content":{marker_json}}},"finish_reason":null}}]}}\n\n'
                    )
                    yield marker_chunk.encode("utf-8")
                    logger.info(f"[session] ✅ Marker embedded: {original_fp[:8]}... ts={ts[:19]}")
                
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
                            chunks = v2_buffer.on_tool_running(tc_id, parsed_json, completion_id, created, model)
                            for chunk in chunks:
                                yield chunk
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
                                yield _heartbeat_chunk_tpl % (
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
                                f'<summary></summary>\n'
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
                
                # ✅ 修復：追蹤第一個有內容的 chunk，之後才啟動心跳
                if not first_content_sent and data_str:
                    try:
                        pj = json.loads(data_str)
                        delta = pj.get("choices", [{}])[0].get("delta", {})
                        if delta.get("content"):
                            first_content_sent = True
                            logger.info(f"[enhance-v2] First content chunk sent, heartbeat enabled")
                    except (json.JSONDecodeError, IndexError, KeyError):
                        pass
                
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


# ── Memory Self-Protection ────────────────────────────────
# 歷史教訓：Open WebUI 帶大量 tool 結果的對話歷史請求可以輕鬆吃掉數百 MB
# （body 讀入 → json 解析 → sanitize 多份拷貝 → 重新序列化）。
# MemoryMax=2G 只計 RSS 不計 swap-out，無限 swap thrashing 會讓進程凍結。
# 這裡在請求入口主動檢查 RSS：逼近上限時立刻拒接 + gc，讓 Open WebUI 重試，
# 而不是讓整個進程被 swap 拖死。systemd MemorySwapMax=256M 是最後防線。

_MEM_GUARD_BYTES = 1300 * 1024 * 1024  # 1.3GB，留 700MB 給 MemoryMax=2G 緩衝
MAX_REQUEST_BODY = 128 * 1024 * 1024   # 128MB 請求體上限（防超大歷史）
_mem_guard_last_gc = 0.0


def _rss_bytes() -> int:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


def _mem_guard_reject() -> bool:
    """
    記憶體壓力檢查。回傳 True 表示應該拒接請求（503）。
    超過閾值時先 gc.collect() 一次，仍超過才拒接。
    """
    global _mem_guard_last_gc
    if _rss_bytes() < _MEM_GUARD_BYTES:
        return False
    now = time.monotonic()
    if now - _mem_guard_last_gc > 5.0:
        _mem_guard_last_gc = now
        import gc
        collected = gc.collect()
        logger.warning(f"[mem-guard] RSS exceeded {_MEM_GUARD_BYTES//(1024*1024)}MB, "
                       f"gc.collect() freed {collected} objects")
    return _rss_bytes() > _MEM_GUARD_BYTES


async def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        timeout = aiohttp.ClientTimeout(total=600, connect=10, sock_read=600)
        # read_bufsize: vision_analyze 回傳的 base64 圖片可達 3-4MB
        # aiohttp 的 StreamReader._high_water = read_bufsize * 2
        # readline() 的 max_size 預設為 _high_water
        # 設為 4MB 使 _high_water = 8MB，足以容納最大的 base64 圖片
        
        # ✅ 關鍵修復：設定 auto_decompress=False 避免額外的解壓縮開銷
        # 並確保 timer_host 正確設定以支援 backpressure
        _http_session = aiohttp.ClientSession(
            timeout=timeout,
            read_bufsize=4194304,
            auto_decompress=False,  # 避免不必要的解壓縮
        )
    return _http_session


# ── Test Mode: Tool Card Rendering Samples ─────────────────
# ⚠️ 必須放在 catch-all route 之前，否則會被捕獲！

@APP.get("/test-tool-cards")
async def test_tool_cards():
    """
    測試模式：直接輸出各種 <details> 格式的 SSE stream，
    讓使用者在 Open WebUI 前端觀察渲染效果。
    """
    import time as _time
    from test_mode import generate_test_stream

    completion_id = f"chatcmpl-{int(_time.time()*1000)}"
    created_ts = int(_time.time())

    return StreamingResponse(
        generate_test_stream(completion_id, created_ts, "test-model"),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )


# ── Route: Catch-all proxy ────────────────────────────────

@APP.api_route("/{port_prefix}/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_with_transform(request: Request, port_prefix: str, rest: str):
    """
    Main proxy route — routes to appropriate handler based on path.
    
    Routes:
    - /{port}/v1/responses/** → ResponsesHandler
    - /{port}/v1/chat/completions → CompletionsHandler (enhance-v2)
    - Other paths → passthrough
    """
    upstream_port = port_prefix
    original_path = f"/{port_prefix}/{rest}"
    upstream_url = resolve_upstream(original_path)

    # ── Memory self-protection: reject under pressure before reading body ──
    if _mem_guard_reject():
        return JSONResponse(
            content={"error": {"message": "Proxy under memory pressure, retry later",
                               "type": "proxy_error", "code": "memory_pressure"}},
            status_code=503,
            headers={"Retry-After": "5"},
        )

    # ── Request body cap: refuse pathological request bodies ──
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_REQUEST_BODY:
        return JSONResponse(
            content={"error": {"message": f"Request body exceeds {MAX_REQUEST_BODY // (1024*1024)}MB limit",
                               "type": "proxy_error", "code": "body_too_large"}},
            status_code=413,
        )

    body = await request.body()
    if len(body) > MAX_REQUEST_BODY:
        return JSONResponse(
            content={"error": {"message": f"Request body exceeds {MAX_REQUEST_BODY // (1024*1024)}MB limit",
                               "type": "proxy_error", "code": "body_too_large"}},
            status_code=413,
        )

    # Build forwarded headers — include Hermes session headers for stateful mode
    fwd_headers = {}
    for hn, hv in request.headers.items():
        hl = hn.lower()
        if hl in ("authorization", "content-type", "x-hermes-session-id", "x-hermes-session-key"):
            fwd_headers[hn] = hv

    # Parse request body
    try:
        req_json = json.loads(body) if body else {}
    except json.JSONDecodeError:
        req_json = {}

    sess = await get_session()

    # ── Route to appropriate handler ──
    if "/v1/responses" in original_path:
        return await handle_responses_request(
            request, upstream_url, fwd_headers, body, req_json, sess, CONFIG
        )
    elif "/v1/chat/completions" in original_path:
        # ✅ Session Isolation: derive/inject session ID if marker mode is enabled
        hermes_sid = request.headers.get("X-Hermes-Session-Id", "").strip()
        if "messages" in req_json and isinstance(req_json["messages"], list):
            if _session_isolation_enabled():
                hermes_sid = get_or_create_session_id(req_json["messages"])
            
            # ✅ Client-side [comp] compression: truncate tool results if [comp] triggered
            result = compress_tool_results(req_json["messages"], CONFIG)
            req_json["messages"] = result[0]
            is_comp_only = result[1]
            
            # ✅ If user sent ONLY [comp], return auto-reply directly without LLM
            if is_comp_only:
                model = req_json.get("model", "hermes-agent")
                completion_id = f"chatcmpl-{int(time.time()*1000)}"
                created_ts = int(time.time())
                logger.info(f"[comp] Auto-reply triggered - returning compressed context directly")
                auto_reply = CONFIG.get("comp_auto_reply", _COMP_AUTO_REPLY)
                return _build_comp_auto_reply_stream(auto_reply, model, completion_id, created_ts)
            
            # ✅ Conversation Compression: compress messages before forwarding
            req_json["messages"] = compress_request_messages(
                req_json["messages"], hermes_sid, CONFIG
            )
            
            # ✅ Component 4: Session Marker Detection & History Injection (Native Tool Context)
            if TOOL_MODE == "native_passthrough" and hermes_sid:
                marker_info = native_tool_context.detect_session_marker(req_json["messages"])
                if marker_info:
                    detected_sid, ts = marker_info
                    target_sid = detected_sid if detected_sid == hermes_sid else hermes_sid
                    try:
                        db = native_tool_context.get_tool_context_db()
                        tool_results = await db.get_tool_results_by_session(target_sid)
                        if tool_results:
                            req_json["messages"] = native_tool_context.inject_tool_results_into_history(
                                req_json["messages"], target_sid, tool_results
                            )
                    except Exception as e:
                        logger.warning(f"[tool-context] Failed to inject history: {e}")
        
        return await handle_completions_request(
            request, upstream_url, fwd_headers, body, req_json, sess,
            upstream_port, sanitize_request_messages, transform_stream,
            hermes_sid,
        )
    else:
        # Passthrough for other endpoints (/v1/models, etc.)
        return await _passthrough(request, upstream_url, fwd_headers, body, sess)


@APP.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_default(request: Request, rest: str):
    """
    Fallback proxy route for paths WITHOUT port prefix.
    Routes to default upstream (30000).
    """
    original_path = f"/{rest}"
    upstream_url = resolve_upstream(original_path)

    # ── Memory self-protection + body cap (same as proxy_with_transform) ──
    if _mem_guard_reject():
        return JSONResponse(
            content={"error": {"message": "Proxy under memory pressure, retry later",
                               "type": "proxy_error", "code": "memory_pressure"}},
            status_code=503,
            headers={"Retry-After": "5"},
        )
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_REQUEST_BODY:
        return JSONResponse(
            content={"error": {"message": f"Request body exceeds {MAX_REQUEST_BODY // (1024*1024)}MB limit",
                               "type": "proxy_error", "code": "body_too_large"}},
            status_code=413,
        )

    body = await request.body()
    if len(body) > MAX_REQUEST_BODY:
        return JSONResponse(
            content={"error": {"message": f"Request body exceeds {MAX_REQUEST_BODY // (1024*1024)}MB limit",
                               "type": "proxy_error", "code": "body_too_large"}},
            status_code=413,
        )

    # Build forwarded headers — include Hermes session headers for stateful mode
    fwd_headers = {}
    for hn, hv in request.headers.items():
        hl = hn.lower()
        if hl in ("authorization", "content-type", "x-hermes-session-id", "x-hermes-session-key"):
            fwd_headers[hn] = hv

    try:
        req_json = json.loads(body) if body else {}
    except json.JSONDecodeError:
        req_json = {}

    sess = await get_session()
    upstream_port = "30000"  # default

    # ── Route to appropriate handler ──
    if "/v1/responses" in original_path:
        return await handle_responses_request(
            request, upstream_url, fwd_headers, body, req_json, sess, CONFIG
        )
    elif "/v1/chat/completions" in original_path:
        # ✅ Session Isolation: derive/inject session ID if marker mode is enabled
        hermes_sid = request.headers.get("X-Hermes-Session-Id", "").strip()
        if "messages" in req_json and isinstance(req_json["messages"], list):
            if _session_isolation_enabled():
                hermes_sid = get_or_create_session_id(req_json["messages"])
            
            # ✅ Client-side [comp] compression: truncate tool results if [comp] triggered
            result = compress_tool_results(req_json["messages"], CONFIG)
            req_json["messages"] = result[0]
            is_comp_only = result[1]
            
            # ✅ If user sent ONLY [comp], return auto-reply directly without LLM
            if is_comp_only:
                model = req_json.get("model", "hermes-agent")
                completion_id = f"chatcmpl-{int(time.time()*1000)}"
                created_ts = int(time.time())
                logger.info(f"[comp] Auto-reply triggered - returning compressed context directly")
                auto_reply = CONFIG.get("comp_auto_reply", _COMP_AUTO_REPLY)
                return _build_comp_auto_reply_stream(auto_reply, model, completion_id, created_ts)
            
            # ✅ Conversation Compression: compress messages before forwarding
            req_json["messages"] = compress_request_messages(
                req_json["messages"], hermes_sid, CONFIG
            )
            
            # ✅ Component 4: Session Marker Detection & History Injection (Native Tool Context)
            if TOOL_MODE == "native_passthrough" and hermes_sid:
                marker_info = native_tool_context.detect_session_marker(req_json["messages"])
                if marker_info:
                    detected_sid, ts = marker_info
                    target_sid = detected_sid if detected_sid == hermes_sid else hermes_sid
                    try:
                        db = native_tool_context.get_tool_context_db()
                        tool_results = await db.get_tool_results_by_session(target_sid)
                        if tool_results:
                            req_json["messages"] = native_tool_context.inject_tool_results_into_history(
                                req_json["messages"], target_sid, tool_results
                            )
                    except Exception as e:
                        logger.warning(f"[tool-context] Failed to inject history: {e}")
        return await handle_completions_request(
            request, upstream_url, fwd_headers, body, req_json, sess,
            upstream_port, sanitize_request_messages, transform_stream,
            hermes_sid,
        )
    else:
        # Passthrough for other endpoints
        return await _passthrough(request, upstream_url, fwd_headers, body, sess)


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
