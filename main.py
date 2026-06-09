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
from tool_history_format import (
    flatten_json,
    format_tool_history_block,
    _format_args_flat,
    _format_result_flat,
)

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


# ── History Sanitization (Anti-pollution) ─────────────────
#
# 問題：hermes_tool_filter 注入的 <details> 標籤以 delta.content 純文字形式
# 進入 Open WebUI 的對話歷史。下次請求時，這些標籤會完整出現在模型的 prompt 中，
# 導致模型模仿輸出 <details> 格式，形成污染反饋迴圈。
#
# 解決：在把請求轉發到 upstream 之前，掃描 messages 中的 assistant content，
# 把 <details type="tool_calls"> 區塊轉換為安全的格式。
#
# 配置：config.yaml 中的 enable_history_sanitization, sanitization_result_max_length, tool_history_format
#
# tool_history_format:
#   legacy — 自然語言描述（舊版）
#   flat   — [START_PREV_ACTION] k:v 格式（新版，防止 JSON 污染）
#   → 實作在 tool_history_format.py 中


def _get_sanitization_config() -> tuple:
    """取得 sanitization 配置，回傳 (enabled: bool, max_result_length: int, format: str)"""
    enabled = CONFIG.get("enable_history_sanitization", True)
    max_length = CONFIG.get("sanitization_result_max_length", 2000)
    fmt = CONFIG.get("tool_history_format", "flat")
    return bool(enabled), int(max_length), str(fmt)


def _extract_tool_info(tag: str, max_result_length: int) -> dict:
    """
    從 <details> 標籤中提取工具資訊。
    
    回傳: {tool_name, args_summary, args_obj, result_summary, result_raw, truncated}
    """
    # 提取 name 屬性（支援雙引號、單引號、無引號、大小寫不敏感）
    name_match = re.search(r'name=["\']?([^"\'>\s]*)["\']?', tag, flags=re.IGNORECASE)
    tool_name = html.unescape(name_match.group(1)) if name_match else "unknown"
    
    # 提取 <arguments> 內容
    args_match = re.search(r'<arguments>(.*?)</arguments>', tag, re.DOTALL)
    args_summary = ""
    args_obj = None
    if args_match:
        args_raw = html.unescape(args_match.group(1).strip())
        try:
            args_obj = json.loads(args_raw)
            # 移除 tool_name/label 等元資料欄位
            clean_args = {k: v for k, v in args_obj.items() 
                        if k not in ("tool_name", "label")}
            if clean_args:
                # 只取第一個有意義的參數作為摘要（legacy 格式用）
                for k, v in clean_args.items():
                    if isinstance(v, str) and len(v) < 100:
                        args_summary = f"查詢「{v}」"
                        break
                    elif isinstance(v, (int, float, bool)):
                        args_summary = f"參數 {k}={v}"
                        break
                else:
                    args_summary = json.dumps(clean_args, ensure_ascii=False)[:100]
        except json.JSONDecodeError:
            args_summary = args_raw[:100]
            args_obj = None
    
    # 提取 <result> 內容
    result_match = re.search(r'<result>(.*?)</result>', tag, re.DOTALL)
    result_summary = ""
    result_raw = ""
    truncated = False
    if result_match:
        result_raw = html.unescape(result_match.group(1).strip())
        # 嘗試解析 JSON 並提取關鍵資訊
        try:
            result_obj = json.loads(result_raw)
            # 如果是成功回應，提取核心數據
            if isinstance(result_obj, dict):
                # 移除巢狀的 result 包裝
                if "result" in result_obj and isinstance(result_obj["result"], str):
                    inner = result_obj["result"]
                    try:
                        inner_obj = json.loads(inner)
                        result_summary = json.dumps(inner_obj, ensure_ascii=False)
                    except json.JSONDecodeError:
                        result_summary = inner
                elif "data" in result_obj:
                    result_summary = json.dumps(result_obj["data"], ensure_ascii=False)
                elif "success" in result_obj:
                    result_summary = json.dumps(result_obj, ensure_ascii=False)
                else:
                    result_summary = json.dumps(result_obj, ensure_ascii=False)
            else:
                result_summary = str(result_obj)
        except json.JSONDecodeError:
            result_summary = result_raw
        
        # 截斷過長的結果
        if len(result_summary) > max_result_length:
            result_summary = result_summary[:max_result_length] + "..."
            truncated = True
            logger.debug(
                f"[sanitization] Result truncated for tool '{tool_name}': "
                f"{len(result_summary)} -> {max_result_length + 3} chars"
            )
    
    return {
        "tool_name": tool_name,
        "args_summary": args_summary,
        "args_obj": args_obj,
        "result_summary": result_summary,
        "result_raw": result_raw,
        "truncated": truncated,
    }


def _generate_natural_description(info: dict, seed: int = 0, index: int = 0) -> str:
    """
    根據工具資訊生成自然語言描述，使用確定性隨機選擇風格。
    
    核心設計原則：
    1. 不使用固定模板格式（避免模型模仿）
    2. 使用多種句式變化
    3. 描述更像「對話中的上下文回顧」而非「工具呼叫紀錄」
    4. 結果部分保留完整數據供模型使用
    5. **確定性隨機**：相同 seed + index 產生相同結果，確保 vLLM KV cache 命中
    
    :param seed: 基於訊息內容 hash 的 seed，相同請求永遠相同
    :param index: 同一訊息中第幾個 <details> 標籤（0-based）
    """
    tool_name = info["tool_name"]
    args_summary = info["args_summary"]
    result_summary = info["result_summary"]
    
    # 根據工具類型選擇適當的描述風格
    tool_type = _classify_tool(tool_name)
    
    # 定義多種自然語言風格
    if tool_type == "search":
        styles = [
            f"先前搜尋了{args_summary}，找到以下結果：{result_summary}",
            f"根據搜尋{args_summary}的結果：{result_summary}",
            f"搜尋{args_summary}後獲得的資訊：{result_summary}",
            f"已查詢{args_summary}，回傳：{result_summary}",
        ]
    elif tool_type == "trading":
        styles = [
            f"先前查詢了交易{args_summary}，數據顯示：{result_summary}",
            f"根據交易工具的回應{args_summary}：{result_summary}",
            f"工具回傳的交易資料{args_summary}：{result_summary}",
            f"從交易系統取得的{args_summary}資料：{result_summary}",
        ]
    elif tool_type == "file":
        styles = [
            f"讀取了檔案內容{args_summary}：{result_summary}",
            f"檔案{args_summary}的內容如下：{result_summary}",
            f"從檔案中讀取到的資料{args_summary}：{result_summary}",
        ]
    elif tool_type == "code":
        styles = [
            f"執行了程式碼{args_summary}，輸出：{result_summary}",
            f"程式碼執行結果{args_summary}：{result_summary}",
            f"程式碼回傳：{result_summary}",
        ]
    else:
        styles = [
            f"先前使用了{tool_name}工具{args_summary}，得到：{result_summary}",
            f"根據{tool_name}工具的回應{args_summary}：{result_summary}",
            f"工具{tool_name}回傳的資料{args_summary}：{result_summary}",
            f"系統已執行{tool_name}{args_summary}，結果為：{result_summary}",
            f"歷史上下文：{tool_name}查詢{args_summary}後的結果：{result_summary}",
        ]
    
    # 使用確定性隨機：相同 seed + index → 相同風格
    # 這樣同一個請求的 sanitization 結果永遠一致，KV cache 才能命中
    rng = random.Random(seed + index)
    return rng.choice(styles)


def _classify_tool(tool_name: str) -> str:
    """根據工具名稱分類工具類型"""
    search_tools = ["web_search", "brave_web_search", "search_files", "session_search"]
    trading_tools = ["mcp_trading_get_positions", "mcp_trading_get_wallet_balance",
                    "mcp_trading_get_market_data", "mcp_trading_create_order"]
    file_tools = ["read_file", "write_file", "patch"]
    code_tools = ["execute_code", "terminal"]
    
    tn = tool_name.lower()
    if any(s in tn for s in search_tools):
        return "search"
    elif any(s in tn for s in trading_tools):
        return "trading"
    elif any(s in tn for s in file_tools):
        return "file"
    elif any(s in tn for s in code_tools):
        return "code"
    else:
        return "general"


def sanitize_message_content(content: str | None, seed: int = 0) -> tuple:
    """
    移除 assistant message 中的 <details type="tool_calls"> 區塊，
    替換為安全格式（flat 或 legacy），切斷污染反饋迴圈。
    
    回傳: (sanitized_content: str, replacement_count: int)
    
    :param seed: 用於確定性隨機的 seed，確保相同內容產生相同結果（KV cache 友好）
    """
    if not content:
        return content, 0
    
    enabled, max_result_length, fmt = _get_sanitization_config()
    if not enabled:
        return content, 0
    
    total_replacements = 0
    detail_index = 0  # 追蹤同一訊息中第幾個 <details>
    
    def _replace_details(m: re.Match) -> str:
        nonlocal total_replacements, detail_index
        total_replacements += 1
        idx = detail_index
        detail_index += 1
        info = _extract_tool_info(m.group(0), max_result_length)
        
        if fmt == "flat":
            # 新格式：[START_PREV_ACTION] k:v 格式
            return format_tool_history_block(
                tool_name=info["tool_name"],
                args=info["args_obj"],
                result_raw=info["result_raw"] or info["result_summary"],
                max_result_length=max_result_length,
            )
        else:
            # 舊格式：自然語言描述
            return _generate_natural_description(info, seed, idx)
    
    # 1. 匹配 <details ... type="tool_calls" ...>...</details>（標準格式，支援多行、屬性順序不限）
    pattern1 = r'<details[^>]*type=["\']?tool_calls["\']?[^>]*>.*?</details>'
    sanitized = re.sub(pattern1, _replace_details, content, flags=re.DOTALL | re.IGNORECASE)
    
    # 2. 處理沒有 type="tool_calls" 屬性的 <details>（模型自己模仿輸出的格式）
    # 只處理包含 <arguments> 或 <result> 子標籤的（明顯是工具相關）
    pattern2 = r'<details[^>]*>\s*\n\s*<summary>.*?</summary>.*?<arguments>.*?</arguments>.*?<result>.*?</result>.*?\n\s*</details>'
    sanitized = re.sub(pattern2, _replace_details, sanitized, flags=re.DOTALL | re.IGNORECASE)
    
    # 3. 兜底：任何包含 <arguments> 和 <result> 的 <details> 區塊
    pattern3 = r'<details[^>]*>.*?<arguments>.*?</arguments>.*?<result>.*?</result>.*?</details>'
    sanitized = re.sub(pattern3, _replace_details, sanitized, flags=re.DOTALL | re.IGNORECASE)
    
    return sanitized, total_replacements


def sanitize_request_messages(messages: list) -> list:
    """
    掃描並清理請求中的所有 messages，防止 <details> 污染。
    只處理 assistant role 的 content。
    
    可透過 config.yaml 的 enable_history_sanitization 開關控制。
    
    **確定性隨機**：每個訊息使用自身內容的 hash 作為 seed，
    確保相同請求的 sanitization 結果一致，vLLM KV cache 才能命中。
    """
    if not messages:
        return messages
    
    enabled, _, _ = _get_sanitization_config()
    if not enabled:
        return messages
    
    total_details_cleaned = 0
    messages_cleaned = 0
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("content"):
            original = msg["content"]
            # 使用訊息內容的 hash 作為 seed，確保相同內容產生相同結果
            seed = hash(original) & 0xFFFFFFFF
            sanitized, count = sanitize_message_content(original, seed)
            msg["content"] = sanitized
            if count > 0:
                messages_cleaned += 1
                total_details_cleaned += count
    
    if total_details_cleaned > 0:
        logger.info(
            f"[sanitization] Replaced {total_details_cleaned} <details> tag(s) "
            f"across {messages_cleaned} message(s) from {len(messages)} total messages"
        )
    
    return messages


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
    """
    safe_name = html.escape(tool_name) if tool_name else "unknown"
    
    attrs = f'type="tool_calls" done="true" name="{safe_name}"'
    
    # <summary> 包含工具名稱 + emoji（供視覺渲染）
    emoji = get_tool_emoji(tool_name)
    display_name = label if label else tool_name
    safe_display = html.escape(display_name)
    inner = f"\n<summary>✅ {emoji} {safe_display}</summary>"
    
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
    
    # ✅ 修復：追蹤是否已發送第一個有內容的 chunk，避免心跳干擾
    first_content_sent = False
    
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
    
    # 主循環
    while True:
        # ✅ 防火牆優化：在等待第一塊內容時使用更短的心跳間隔
        current_heartbeat_interval = initial_wait_interval if not first_content_sent else heartbeat_interval
        
        # 心跳檢查
        elapsed = time.monotonic() - last_heartbeat
        if elapsed >= current_heartbeat_interval:
            if not first_content_sent:
                # 還在等待第一塊內容，發送 SSE comment 保持連接活躍
                heartbeat_count += 1
                yield b': keepalive-waiting-first-chunk\n\n'
                last_heartbeat = time.monotonic()
                logger.debug(f"[firewall-optimization] Sent keepalive while waiting for first chunk (count={heartbeat_count})")
            elif elapsed >= heartbeat_interval:
                # 正常心跳（第一個 content 之後）
                heartbeat_count += 1
                yield b': keepalive\n\n'
                last_heartbeat = time.monotonic()
                tool_just_completed = False

        # 非阻塞讀取 — 使用 asyncio.wait_for 確保不會永久阻塞
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=1.0)
        except asyncio.TimeoutError:
            # 超時了，繼續循環，下次心跳會發送
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


async def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        timeout = aiohttp.ClientTimeout(total=600, connect=10, sock_read=600)
        _http_session = aiohttp.ClientSession(timeout=timeout, max_line_size=65536)
    return _http_session


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

    body = await request.body()

    # Build forwarded headers
    fwd_headers = {}
    for hn, hv in request.headers.items():
        hl = hn.lower()
        if hl in ("authorization", "content-type"):
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
        return await handle_completions_request(
            request, upstream_url, fwd_headers, body, req_json, sess,
            upstream_port, sanitize_request_messages, transform_stream,
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

    sess = await get_session()
    upstream_port = "30000"  # default

    # ── Route to appropriate handler ──
    if "/v1/responses" in original_path:
        return await handle_responses_request(
            request, upstream_url, fwd_headers, body, req_json, sess, CONFIG
        )
    elif "/v1/chat/completions" in original_path:
        return await handle_completions_request(
            request, upstream_url, fwd_headers, body, req_json, sess,
            upstream_port, sanitize_request_messages, transform_stream,
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
