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
import signal
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, AsyncGenerator, List

import aiohttp
from aiohttp.http_exceptions import LineTooLong
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

# ── Crash Debug: SIGUSR2 thread dump ────────────────────────
# 當 process 卡死時，發送 SIGUSR2 會立刻 dump 所有執行緒堆疊到 log 檔案。
# 用法: kill -USR2 <pid>
# 這是在 D state 時唯一能拿到現場資料的方法（D state 下 Python 回調可能跑不起來）。

def _thread_dump_handler(signum, frame):
    """SIGUSR2 handler: dump all thread stacks to log file."""
    dump_lines = ["=" * 80, "CRASH DUMP triggered by SIGUSR2 at", time.strftime("%Y-%m-%d %H:%M:%S"), "=" * 80]
    
    # 1. Process status
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if any(line.startswith(k) for k in ["Name", "State", "Threads", "VmRSS", "VmSize", "VmPeak", "voluntary", "involuntary"]):
                    dump_lines.append(line.rstrip())
    except Exception as e:
        dump_lines.append(f"[ERROR reading /proc/self/status] {e}")
    
    # 2. Thread stacks
    dump_lines.append("")
    dump_lines.append(f"--- {threading.active_count()} active threads ---")
    frames = sys._current_frames()
    for tid, frame in frames.items():
        t = None
        for t in threading.enumerate():
            if t.ident == tid:
                break
        tname = t.name if t else f"tid={tid}"
        dump_lines.append(f"\n### Thread: {tname} (tid={tid}) ###")
        stack = traceback.format_stack(frame)
        dump_lines.extend(stack)
    
    # 3. asyncio task info
    try:
        loop = asyncio.get_event_loop()
        tasks = asyncio.all_tasks(loop)
        dump_lines.append(f"\n--- asyncio tasks: {len(tasks)} ---")
        for task in list(tasks)[:20]:  # 最多 20 個
            dump_lines.append(f"  Task: {task.get_name() if hasattr(task, 'get_name') else repr(task)}")
            if task.done():
                dump_lines.append(f"    Status: DONE")
            else:
                dump_lines.append(f"    Status: {'CANCELLED' if task.cancelled() else 'PENDING/RUNNING'}")
    except Exception as e:
        dump_lines.append(f"[ERROR reading asyncio tasks] {e}")
    
    dump_text = "\n".join(dump_lines)
    # 直接寫檔案（不經過 logging，因為 D state 下 logging 可能也卡住）
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(dump_text + "\n")
    except Exception:
        pass


# Register SIGUSR2 handler (only in main thread)
try:
    signal.signal(signal.SIGUSR2, _thread_dump_handler)
except (OSError, ValueError):
    pass  # Not main thread or signal unavailable


# ── Crash Debug: Periodic health dump ──────────────────────
# 背景任務：每 30 秒 dump 一次關鍵指標到 log 檔案。
# 包含：RSS、buffer size、active tools、asyncio tasks、thread count。

_health_dump_interval = 30  # seconds
_health_dump_task = None

# ── RSS watchdog 閾值（2026-08-07 新增）──
# stale stream 曾吃到 3.2G RSS + 1G swap；3GB 主動退出，比 systemd
# MemoryMax=4G 硬殺更早，避免 swap thrashing。Restart=always 會重啟。
_RSS_WATCHDOG_BYTES = 3 * 1024 * 1024 * 1024  # 3GB


async def _health_dump_loop():
    """Periodic health dump task — runs in background."""
    global _health_dump_task
    _health_dump_task = asyncio.current_task()
    while True:
        await asyncio.sleep(_health_dump_interval)
        try:
            rss_kb = 0
            with open("/proc/self/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        break
            
            # ── RSS watchdog：超過上限直接自殺讓 systemd 重啟 ──
            # 歷史教訓（2026-08-07）：stale stream 20 分鐘吃到 3.2G RSS +
            # 1G swap，swap thrashing 會拖垮整台機器（先前 1.4TB 讀取事件）。
            # 與其等 systemd MemoryMax=4G 硬殺，不如在 3G 就主動退出——
            # Restart=always 會拉起來，Open WebUI 重試即可。
            if rss_kb * 1024 >= _RSS_WATCHDOG_BYTES:
                logger.critical(
                    f"[rss-watchdog] RSS={rss_kb}kB >= 3GB limit. "
                    f"Self-terminating to prevent swap thrashing. "
                    f"threads={threading.active_count()} "
                    f"asyncio-tasks={len(asyncio.all_tasks())}"
                )
                # flush log 後退出（exit code 1 → systemd Restart=always 重啟）
                for handler in logger.handlers:
                    try:
                        handler.flush()
                    except Exception:
                        pass
                os._exit(1)
            
            # Count active streams (approximate via open file descriptors to 127.0.0.1)
            task_count = len(asyncio.all_tasks()) if asyncio.get_event_loop().is_running() else 0
            
            logger.info(
                f"[health-dump] RSS={rss_kb}kB threads={threading.active_count()} "
                f"asyncio-tasks={task_count}"
            )
        except Exception as e:
            logger.warning(f"[health-dump] error: {e}")


async def start_health_dump():
    """Start the periodic health dump background task."""
    asyncio.create_task(_health_dump_loop())
    logger.info(f"[health-dump] Started periodic dump every {_health_dump_interval}s")

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
    _perf_t0 = time.monotonic()
    if not _comp_mode_enabled(config):
        _elapsed = (time.monotonic() - _perf_t0) * 1000
        logger.debug(f"[perf] compress_tool_results SKIPPED (comp_mode disabled) {_elapsed:.1f}ms")
        return (messages, False)
    
    if not messages:
        _elapsed = (time.monotonic() - _perf_t0) * 1000
        logger.debug(f"[perf] compress_tool_results SKIPPED (empty messages) {_elapsed:.1f}ms")
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
    
    _elapsed = (time.monotonic() - _perf_t0) * 1000
    logger.info(f"[perf] compress_tool_results DONE in {_elapsed:.1f}ms (messages={len(messages)}, compressed={total_compressed})")
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
    _perf_t0 = time.monotonic()
    if not messages:
        _elapsed = (time.monotonic() - _perf_t0) * 1000
        logger.debug(f"[perf] compress_request_messages SKIPPED (empty) {_elapsed:.1f}ms")
        return messages

    mode = config.get("compression_mode", "server-side")
    if mode != "server-side" or not hermes_sid:
        _elapsed = (time.monotonic() - _perf_t0) * 1000
        logger.debug(f"[perf] compress_request_messages SKIPPED (mode={mode}, sid={'yes' if hermes_sid else 'no'}) {_elapsed:.1f}ms")
        return messages

    if len(messages) <= 2:
        _elapsed = (time.monotonic() - _perf_t0) * 1000
        logger.debug(f"[perf] compress_request_messages SKIPPED (len<=2) {_elapsed:.1f}ms")
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
    _elapsed = (time.monotonic() - _perf_t0) * 1000
    logger.info(f"[perf] compress_request_messages DONE in {_elapsed:.1f}ms (msgs {original_count}->{len(compressed)}, chars {original_size}->{compressed_size})")
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


# 圖片參數鍵（不含裸 path/paths，避免 read_file 被誤判）
_IMAGE_ARG_KEYS = {
    "image_url", "image_urls", "image_path", "image_paths",
    "screenshot_path", "screenshot_paths", "images", "image",
}
_DATA_URL_RE = re.compile(
    r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]{64,}",
    re.MULTILINE,
)


def _sanitize_progress_arguments(arguments: Any) -> Optional[dict]:
    """Drop base64 / oversized values from progress arguments before tool cards."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
    if not isinstance(arguments, dict):
        return None
    clean: Dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and (
            value.startswith("data:image")
            or ("base64" in value[:80] and len(value) > 2000)
            or len(value) > 8000
        ):
            clean[key] = f"[redacted {len(value)} chars]"
        else:
            clean[key] = value
    return clean


def _sanitize_progress_result(result: Any) -> str:
    """
    Ensure progress result never keeps multi-MB base64 in filter memory/UI.

    Prefer gateway-side redaction; this is defense-in-depth if upstream still
    dumps a native vision envelope onto hermes.tool.progress.
    """
    if result is None:
        return ""
    if isinstance(result, dict):
        if result.get("_multimodal") or result.get("_multimodal_redacted"):
            summary = result.get("text_summary") or result.get("text") or ""
            raw_meta = result.get("meta")
            meta = raw_meta if isinstance(raw_meta, dict) else {}
            size = meta.get("size_bytes")
            size_note = f" ({size/1024:.1f}KB)" if isinstance(size, (int, float)) else ""
            if summary:
                return f"{summary}{size_note}"
            return f"Image loaded natively{size_note}; base64 redacted from tool card."
        try:
            result = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            result = str(result)

    result_str = result if isinstance(result, str) else str(result)
    original_len = len(result_str)

    # Fast multimodal envelope detection without full json.loads of multi-MB blobs
    head = result_str[:240]
    if '"_multimodal"' in head or "'_multimodal'" in head or '"_multimodal_redacted"' in head:
        # text_summary is usually AFTER the huge content[].image_url base64 —
        # search head+tail only, never scan the whole multi-MB string with json.loads.
        sample = (
            result_str[:4000] + result_str[-6000:]
            if original_len > 10000
            else result_str
        )
        m = re.search(r'"text_summary"\s*:\s*"((?:\\.|[^"\\])*)"', sample)
        if m:
            try:
                summary = json.loads(f'"{m.group(1)}"')
            except Exception:
                summary = m.group(1)
            logger.info(
                f"[multimodal] stripped envelope result_len={original_len} "
                f"-> summary_len={len(summary)}"
            )
            return f"{summary}（圖片已經從你的上下文移除 你的下一輪回答將不會具有圖片知識 若用戶詢問有關內容 需要再調用一次圖片工具）"
        logger.info(f"[multimodal] stripped envelope result_len={original_len} (no summary)")
        return f"圖片已載入模型上下文（圖片已經從你的上下文移除 你的下一輪回答將不會具有圖片知識 若用戶詢問有關內容 需要再調用一次圖片工具）"

    if "data:image" in result_str or original_len > 100_000:
        redacted = _DATA_URL_RE.sub("[base64_image_redacted]", result_str)
        if len(redacted) > 20000:
            redacted = redacted[:20000] + f"...[truncated from {original_len} chars]"
        logger.info(
            f"[multimodal] redacted data-url/large result {original_len} -> {len(redacted)} chars"
        )
        return redacted

    return result_str


def _neutralize_details_tags(text: str) -> str:
    """
    把 body 裡的 <details / </details> 字串 escape 掉。

    OWUI detailsTokenizer 的 findMatchingClosingTag 是純字串深度計數
    （不認識 code fence），body 內若出現 </details> 會把 token 提前截斷，
    其後的 <details ...> 會被當成真卡片渲染（注入）。
    把 < 換成 &lt; 即可打破字串匹配；OWUI 渲染時 decode() 會還原，
    再經 code fence escape，顯示仍是乾淨的原文。
    """
    return text.replace("<details", "&lt;details").replace("</details>", "&lt;/details&gt;")


def _wrap_in_code_fence(text: str) -> str:
    """
    把文字包進 fenced code block，防止內容跳脫。

    動機：result 裡若含 ``` 代碼塊、</details>、<script> 等字元，
    直接當 markdown 渲染會破壞 <details> 結構（提前閉合 / HTML 注入 /
    代碼塊嵌套錯亂）。包進 code fence 後，所有 < > ` 都只當純文字顯示。

    fence 長度會比內容中最長的連續 backtick 還長一截，
    避免內容自帶 ``` 時提前關閉外層 fence。
    """
    if not text:
        return text
    max_run = run = 0
    for ch in text:
        if ch == "`":
            run += 1
            if run > max_run:
                max_run = run
        else:
            run = 0
    fence = "`" * max(3, max_run + 1)
    return f"{fence}\n{text}\n{fence}"


def _build_completion_details(tool_name: str, label: str = "", result: str = "", arguments: Optional[dict] = None) -> str:
    """
    Build a complete <details> tag for a completed tool call.

    格式（attribute-based，與 Conduit v4.1.0 + OWUI 0.10.2 前端對齊）：
      <details type="tool_calls" done="true" name="tool" arguments="{...}">
      <summary></summary>
      result text here
      </details>

    - arguments 放在 attribute（JSON，短、無換行，前端 parseAttributes 直接讀）
    - result 放在 body（可長、可換行；Conduit DetailsBlockSyntax 會把 body
      normalize 進 result attribute；OWUI detailsTokenizer 的 token.text 也是 body）
    - body 整段包進 fenced code block（fence 長度自動 >= 內容最長 backtick run + 1），
      防止 result 內的 ``` / </details> / <script> 跳脫破壞結構
    - 結果截斷（最多 5000 字元）
    - **多模態處理**：result/_multimodal/data:image 一律消毒，不把 base64 塞進 OWUI
    """
    safe_name = html.escape(tool_name) if tool_name else "unknown"

    # ── 確保 arguments 是 dict，並去掉 base64 ─────────────────
    arguments = _sanitize_progress_arguments(arguments)

    # ── attributes：type / done / name / arguments ─────────────
    # arguments 必須 HTML-escape（quote=True）才能安全嵌在 "..." 裡
    if arguments:
        full_args = {"tool_name": tool_name, **arguments}
    elif label:
        full_args = {"tool_name": tool_name, "label": label}
    else:
        full_args = {"tool_name": tool_name}
    args_str = json.dumps(full_args, ensure_ascii=False)
    args_attr = html.escape(args_str, quote=True)

    attrs = f'type="tool_calls" done="true" name="{safe_name}" arguments="{args_attr}"'

    # ── body：result（plain text，可含換行）────────────────────
    body_parts: list[str] = []
    if result:
        # 先消毒（gateway 應已 redact；這裡是第二道防線）
        result_str = _sanitize_progress_result(result)
        result_len = len(result_str)

        has_image_arg = bool(arguments and (_IMAGE_ARG_KEYS & set(arguments.keys())))
        is_multimodal_marker = (
            "_multimodal" in result_str[:200]
            or "base64_image_redacted" in result_str
            or "base64 已從" in result_str
            or "Image loaded natively" in result_str
        )
        is_large_result = result_len > 10240

        if (has_image_arg and is_large_result) or is_multimodal_marker:
            # 視覺 / 多模態：只留短提示，避免 OWUI 歷史被像素塞爆
            if is_multimodal_marker and result_len <= 5000:
                body_parts.append(result_str)
            else:
                body_parts.append(
                    f"圖片已從 tool card 移除"
                    f"（處理後 {result_len/1024:.1f}KB）。"
                    f"像素只在當輪模型上下文，需要再看請重呼 vision 工具。"
                )
        else:
            truncated = result_str[:5000] + ("..." if result_len > 5000 else "")
            body_parts.append(truncated)

    body = "\n".join(body_parts)
    # 防跳脫兩道：
    # 1. neutralize body 內的 <details / </details>（OWUI tokenizer 是純字串計數，不認 fence）
    # 2. 整段包進 code fence（``` / <script> / <b> 等一律降級為純文字）
    if body:
        body = _neutralize_details_tags(body)
        body = _wrap_in_code_fence(body)

    # 開標籤必須是單行（前端 regex 逐行匹配），所以 attributes 裡不能有換行
    # html.escape 已把 < > & " 都 escape 掉，args_str 是 json.dumps 單行輸出，安全
    return f'<details {attrs}>\n<summary></summary>\n{body}\n</details>'


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

    def on_tool_running(self, tc_id: str, payload: dict,
                        completion_id: str, created: int, model: str) -> List[bytes]:
        """工具開始執行，記錄狀態並立即發送 running 通知以保持 stream 活躍。"""
        _t0 = time.monotonic()
        self._prune()
        tool_name = payload.get("tool", "unknown")
        emoji = payload.get("emoji", get_tool_emoji(tool_name))
        
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
        _elapsed = (time.monotonic() - _t0) * 1000
        logging.info(f"[perf] on_tool_running {tool_name} tc_id={tc_id[:8]}... active={len(self.active_tools)} {_elapsed:.1f}ms")
        return chunks
    
    def on_tool_completed(self, tc_id: str, payload: dict,
                          completion_id: str, created: int, model: str) -> List[bytes]:
        """
        工具完成 → 只注入 <details type="tool_calls" done="true"> 到 content stream。
        這樣 Open WebUI 會正確渲染 tool card，並把 result 存進歷史訊息。
        """
        _t0 = time.monotonic()
        try:
            state = self.active_tools.pop(tc_id, {})
            # Sanitize immediately so multi-MB base64 never sits in buffer/state
            raw_result = payload.get("result", "")
            raw_args = payload.get("arguments", state.get("arguments", {}))
            result = _sanitize_progress_result(raw_result)
            arguments = _sanitize_progress_arguments(raw_args) or {}
            # Drop references to possibly huge upstream payloads ASAP
            del raw_result, raw_args
            
            tool_name = state.get("tool", payload.get("tool", "unknown"))
            
            chunks = []
            
            # ✅ 只注入帶 arguments + result 的 <details>（正確做法）
            emoji = state.get("emoji", get_tool_emoji(tool_name))
            label = state.get("label", tool_name)
            details = _build_completion_details(tool_name, label, result, arguments)
            
            # 加 \n\n 確保 Markdown 正確解析 <details> block
            # 整個 <details> 在一個 chunk 中發出，避免被分割
            chunks.append(_build_content_chunk(f"\n\n{details}\n"))
            
            _elapsed = (time.monotonic() - _t0) * 1000
            logging.info(
                f"[perf] on_tool_completed {tool_name} tc_id={tc_id[:8]}... "
                f"result_len={len(result)} active={len(self.active_tools)} {_elapsed:.1f}ms"
            )
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
    _stream_start = time.monotonic()
    _readline_count = 0
    _last_buffer_dump = time.monotonic()
    
    # ── Stale stream protection ──
    # 歷史教訓（2026-08-07）：Hermes gateway 在 relay finalization 異常後可能
    # 「活著但啞巴」——socket 不關、數據不送、[DONE] 不發。filter 若只等 EOF
    # 會無限 READLINE TIMEOUT 循環（實測 20 分鐘吃 3.2G RAM）。
    # 這裡追蹤最後一次收到 upstream 數據的時間，超時即強制結束 stream。
    _last_data_time = time.monotonic()
    # 120 秒完全無數據 → 判定 upstream 死亡（thinking/長工具執行期間
    # progress 事件會持續進來，正常 stream 不會靜默超過 2 分鐘）
    STALE_STREAM_TIMEOUT = 120.0
    
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

        # ── Crash Debug: timed readline ──
        _readline_count += 1
        _read_start = time.monotonic()
        try:
            # Cap single SSE line. Gateway should already redact multi-MB base64
            # from hermes.tool.progress; this is a backstop so one bad frame
            # cannot blow RSS. 6MB > embed target(~4MB) with JSON overhead, but
            # far below "hold 20MB×N images" territory.
            line = await asyncio.wait_for(
                reader.readuntil(max_size=6 * 1024 * 1024),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            # 超時了，繼續循環，下次心跳會發送
            # DEBUG: 記錄 buffer 狀態，排查 gateway 是否發送數據
            _read_elapsed = time.monotonic() - _read_start

            # ── EOF detection ──
            if reader.at_eof():
                logger.info(
                    f"[enhance-v2] Upstream EOF detected on timeout (stream_age={time.monotonic() - _stream_start:.1f}s, "
                    f"readline_count={_readline_count}). Breaking immediately."
                )
                yield b'data: {"id":"%s","object":"chat.completion.chunk","created":%d,"model":"%s","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n' % (
                    completion_id.encode(), created, model.encode()
                )
                yield b'data: [DONE]\n\n'
                break

            # ── Tight loop protection (critical fix) ──
            # readuntil() 在 half-closed socket 上會瞬間回傳（不等 timeout），
            # 導致每秒數萬次的緊密迴圈。偵測瞬間超時並強制延遲。
            if _read_elapsed < 0.5:
                # 瞬間超時 → 強制等待，打破緊密迴圈
                await asyncio.sleep(1.0)

            # ── Stale stream protection ──
            # upstream 長時間完全無數據 → 判定 gateway 已「啞巴」死亡
            # （relay finalization 異常 / gateway 重啟後舊連線失效）。
            # 強制結束 stream，避免無限 READLINE TIMEOUT 循環吃爆記憶體。
            stale_for = time.monotonic() - _last_data_time
            if stale_for > STALE_STREAM_TIMEOUT:
                logger.error(
                    f"[enhance-v2] STALE STREAM: no upstream data for "
                    f"{stale_for:.0f}s (> {STALE_STREAM_TIMEOUT:.0f}s), "
                    f"stream_age={time.monotonic() - _stream_start:.1f}s "
                    f"readline_count={_readline_count} done={done_received} "
                    f"active_tools={len(v2_buffer.active_tools) if v2_buffer else 0}. "
                    f"Force-ending stream to free memory."
                )
                # 通知下游 stream 結束（避免 Open WebUI 一直轉圈）
                yield b'data: {"id":"%s","object":"chat.completion.chunk","created":%d,"model":"%s","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n' % (
                    completion_id.encode(), created, model.encode()
                )
                yield b'data: [DONE]\n\n'
                break
            
            if not first_content_sent and len(buffer) > 0:
                logger.warning(
                    f"[readline-debug] TIMEOUT but buffer_len={len(buffer)}, "
                    f"buffer_preview={buffer[:200]!r}, first_content_sent={first_content_sent}"
                )
            
            # ── Crash Debug: periodic buffer state dump ──
            now = time.monotonic()
            if now - _last_buffer_dump > 10.0:
                _last_buffer_dump = now
                logger.warning(
                    f"[crash-debug] READLINE TIMEOUT after {_read_elapsed:.2f}s | "
                    f"stream_age={now - _stream_start:.1f}s readline_count={_readline_count} "
                    f"buffer_len={len(buffer)} heartbeat={heartbeat_count} "
                    f"first_content={first_content_sent} done={done_received} "
                    f"active_tools={len(v2_buffer.active_tools) if v2_buffer else 0} "
                    f"stale={time.monotonic() - _last_data_time:.0f}s"
                )
            continue
        except LineTooLong as e:
            # Oversized SSE frame (likely unredacted vision base64). Drain the
            # rest of the line without retaining it, then inject a stub card so
            # the stream survives for Open WebUI.
            logger.error(
                f"[enhance-v2] SSE line too long ({e}); draining remainder and "
                f"skipping frame to protect RAM. Fix: ensure gateway redacts "
                f"multimodal base64 from hermes.tool.progress."
            )
            try:
                while True:
                    chunk = await asyncio.wait_for(reader.read(64 * 1024), timeout=2.0)
                    if not chunk:
                        break
                    if b"\n" in chunk:
                        break
            except Exception as drain_err:
                logger.error(f"[enhance-v2] drain after LineTooLong failed: {drain_err}")
            # Keep OWUI alive with a tiny placeholder
            yield _build_content_chunk(
                "\n\n<details type=\"tool_calls\" done=\"true\" name=\"vision_or_large_tool\" "
                "arguments=\"{&quot;note&quot;: &quot;progress frame dropped (oversized SSE)&quot;}\">"
                "\n<summary></summary>\n"
                "上游 progress 封包過大已丟棄（疑 base64）。"
                "模型當輪上下文不受影響；tool card 僅占位。\n"
                "\n</details>\n"
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

        # ✅ 收到任何 upstream 數據即更新 stale 計時器
        _last_data_time = time.monotonic()

        # Empty line means end of connection — LOG THIS!
        if not line:
            elapsed = time.monotonic() - last_heartbeat
            stream_age = time.monotonic() - _stream_start
            logger.info(
                f"[enhance-v2] Upstream EOF detected! "
                f"stream_age={stream_age:.1f}s readline_count={_readline_count} "
                f"last_heartbeat={elapsed:.1f}s ago, "
                f"done_received={done_received}, "
                f"buffer_len={len(buffer)}, "
                f"tool_just_completed={tool_just_completed}, "
                f"active_tools={len(v2_buffer.active_tools) if v2_buffer else 0}"
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
                        # enhance-v2: 保留所有 content chunk（包括模型輸出的 <details> 字串）
                        # 舊邏輯會丟掉任何包含 'type="tool_calls"' 的 chunk，
                        # 導致模型正常輸出被截斷（例如主人測試原樣貼出 <details> 標籤）。
                        # tool filter 自己注入的 tool card 是獨立 chunk（handle_tool_completion），
                        # 不經過這裡的 content 過濾路徑，所以不需要在這裡防重複。
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
    
    # ── Crash Debug: stream completion summary ──
    stream_age = time.monotonic() - _stream_start
    logger.info(
        f"[crash-debug] STREAM COMPLETE stream_age={stream_age:.1f}s "
        f"readline_count={_readline_count} heartbeat={heartbeat_count} "
        f"active_tools={len(v2_buffer.active_tools) if v2_buffer else 0}"
    )


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
        # read_bufsize: single SSE line soft-cap is handled in transform_stream
        # (readuntil max_size=6MB). Keep session buffer modest — gateway must
        # redact multimodal base64 from hermes.tool.progress so we never need
        # 20MB×N headroom here (that would thrash RAM under MemoryMax).
        # high_water = read_bufsize * 2 = 12MB (above 6MB line cap).
        
        # ✅ 關鍵修復：設定 auto_decompress=False 避免額外的解壓縮開銷
        # 並確保 timer_host 正確設定以支援 backpressure
        _http_session = aiohttp.ClientSession(
            timeout=timeout,
            read_bufsize=6 * 1024 * 1024,
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
    # ── Crash Debug: request entry tracking ──
    req_id = f"{port_prefix}/{rest[:50]}"
    start_time = time.monotonic()
    logger.info(f"[req-trace] ENTER {request.method} /{port_prefix}/{rest[:80]} req_id={req_id}")

    # ── Performance Metrics: stage timers ──
    _perf_stages: Dict[str, float] = {}
    _perf_body_size = 0
    _perf_msg_count = 0
    _perf_msg_chars = 0
    _perf_start = time.monotonic()

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
    _perf_body_time = (time.monotonic() - _perf_start) * 1000
    _perf_stages["body_read"] = _perf_body_time
    try:
        req_json = json.loads(body) if body else {}
    except json.JSONDecodeError:
        req_json = {}

    _perf_body_size = len(body)
    if "messages" in req_json and isinstance(req_json["messages"], list):
        _perf_msg_count = len(req_json["messages"])
        _perf_msg_chars = sum(len(str(m.get("content", ""))) for m in req_json["messages"])

    sess = await get_session()

    # ── Route to appropriate handler ──
    if "/v1/responses" in original_path:
        logger.info(
            f"[perf] REQ body={_perf_body_size}B msgs={_perf_msg_count} chars={_perf_msg_chars} "
            f"body_read={_perf_body_time:.1f}ms req_id={req_id}"
        )
        logger.info(f"[req-trace] ROUTE to responses_handler req_id={req_id}")
        result = await handle_responses_request(
            request, upstream_url, fwd_headers, body, req_json, sess, CONFIG
        )
        _elapsed = time.monotonic() - start_time
        logger.info(
            f"[perf] EXIT responses req_id={req_id} TOTAL={_elapsed:.1f}s "
            f"body={_perf_body_size}B msgs={_perf_msg_count}"
        )
        return result
    elif "/v1/chat/completions" in original_path:
        logger.info(
            f"[perf] REQ body={_perf_body_size}B msgs={_perf_msg_count} chars={_perf_msg_chars} "
            f"body_read={_perf_body_time:.1f}ms req_id={req_id}"
        )
        logger.info(f"[req-trace] ROUTE to completions_handler req_id={req_id}")
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
        
        result = await handle_completions_request(
            request, upstream_url, fwd_headers, body, req_json, sess,
            upstream_port, sanitize_request_messages, transform_stream,
            hermes_sid,
        )
        _elapsed = time.monotonic() - start_time
        logger.info(
            f"[perf] EXIT completions req_id={req_id} TOTAL={_elapsed:.1f}s "
            f"body={_perf_body_size}B msgs={_perf_msg_count} chars={_perf_msg_chars}"
        )
        return result
    else:
        # Passthrough for other endpoints (/v1/models, etc.)
        logger.info(f"[req-trace] ROUTE to passthrough req_id={req_id}")
        result = await _passthrough(request, upstream_url, fwd_headers, body, sess)
        _elapsed = time.monotonic() - start_time
        logger.info(
            f"[perf] EXIT passthrough req_id={req_id} TOTAL={_elapsed:.1f}s "
            f"body={_perf_body_size}B"
        )
        return result


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
    logger.info(f"Crash debug: Periodic health dump enabled (every {_health_dump_interval}s)")
    uvicorn.run(APP, host=BIND_HOST, port=BIND_PORT, log_level="info")
