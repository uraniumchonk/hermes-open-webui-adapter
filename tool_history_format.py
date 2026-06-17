"""
Tool History Format — 給模型看的工具歷史格式。

核心設計：完全避免使用 { } 或 <tag> 標籤，防止模型模仿或觸發 JSON 補全本能。

格式範例:
    [START_PREV_ACTION]
    [ACTION_TYPE]
    search_files
    [ACTION_ARG]
    pattern: *.py
    target: content
    [RESULT]
    total_count: 5
    matches[0].path: main.py
    matches[0].line: 42
    [END_PREV_ACTION]
"""

from __future__ import annotations

import json
import logging
import random
import re
import html
from typing import Any

logger = logging.getLogger(__name__)


def flatten_json(obj: Any, parent_key: str = "", sep: str = ".") -> list[tuple[str, str]]:
    """
    遞迴將 JSON 物件展開成扁平的 (key, value) 對列表。

    設計原則：
    1. 完全不使用 { } 或 JSON 格式
    2. 巢狀鍵用 . 連接（如 data[0].coin）
    3. 列表用 [index] 標記
    4. 複雜型別（dict/list）轉為字串描述而非原始 JSON
    5. 簡單型別（str/int/float/bool/None）直接轉字串

    範例:
        {"data": [{"coin": "BTC", "macd": 65.15}], "status": "ok"}
        → [("status", "ok"), ("data[0].coin", "BTC"), ("data[0].macd", "65.15")]
    """
    items: list[tuple[str, str]] = []

    if isinstance(obj, dict):
        for k, v in obj.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            items.extend(flatten_json(v, new_key, sep))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            new_key = f"{parent_key}[{i}]"
            items.extend(flatten_json(v, new_key, sep))
    else:
        # 葉節點：轉為字串
        if obj is None:
            items.append((parent_key, "null"))
        elif isinstance(obj, bool):
            items.append((parent_key, str(obj).lower()))
        elif isinstance(obj, (int, float)):
            items.append((parent_key, str(obj)))
        else:
            items.append((parent_key, str(obj)))

    return items


def _format_args_flat(args: dict | None, max_length: int = 500) -> str:
    """
    將工具參數轉換為 k:v 格式。
    移除 tool_name/label 等元資料欄位。
    """
    if not args:
        return "(none)"

    # 移除元資料
    clean = {k: v for k, v in args.items() if k not in ("tool_name", "label")}
    if not clean:
        return "(none)"

    flat_pairs = flatten_json(clean)
    if not flat_pairs:
        return "(none)"

    lines = [f"{k}: {v}" for k, v in flat_pairs]
    result = "\n".join(lines)

    if len(result) > max_length:
        result = result[:max_length] + "..."

    return result


def _format_result_flat(result_raw: str, max_length: int = 2000) -> str:
    """
    將工具結果轉換為 k:v 格式。
    嘗試解析 JSON 後展開，解析失敗則直接截斷。
    """
    if not result_raw:
        return "null"

    # 嘗試解析 JSON 並展開
    parsed_obj = None

    # 嘗試 1: 標準 JSON 解析
    try:
        parsed_obj = json.loads(result_raw)
    except json.JSONDecodeError:
        pass

    # 嘗試 2: 修復控制字元後再解析（處理換行符在字串值中的情況）
    if parsed_obj is None:
        try:
            # 用 regex 找到字串值中的裸換行並轉為 \n
            fixed = _fix_json_control_chars(result_raw)
            parsed_obj = json.loads(fixed)
        except json.JSONDecodeError:
            pass

    # 嘗試 3: 如果不是 JSON，直接當純文字
    if parsed_obj is None:
        if len(result_raw) > max_length:
            return result_raw[:max_length] + "..."
        return result_raw

    # 處理常見的包裝層 — 決定要展開的物件
    obj_to_flatten = parsed_obj
    if isinstance(parsed_obj, dict):
        if "result" in parsed_obj and isinstance(parsed_obj["result"], str):
            try:
                obj_to_flatten = json.loads(parsed_obj["result"])
            except json.JSONDecodeError:
                obj_to_flatten = parsed_obj
        elif "data" in parsed_obj:
            obj_to_flatten = parsed_obj["data"]
        # else: 直接用整個物件

    flat_pairs = flatten_json(obj_to_flatten)
    if flat_pairs:
        lines = [f"{k}: {v}" for k, v in flat_pairs]
        result = "\n".join(lines)
    else:
        result = "null"

    if len(result) > max_length:
        result = result[:max_length] + "..."

    return result


def _fix_json_control_chars(s: str) -> str:
    """
    修復 JSON 字串中的控制字元問題。
    當 JSON 字串值中包含實際的換行符（而非 \n 轉義）時，
    json.loads 會報錯。這裡嘗試修復。
    """
    import re as _re
    # 匹配 JSON 字串值（雙引號內），將其中的裸換行替換為 \\n
    def fix_string_match(m):
        content = m.group(1)
        # 將實際的換行符替換為 \\n（但在已經轉義的 \\n 前不額外處理）
        content = content.replace("\\\\", "\x00")  # 保護已有的反斜線
        content = content.replace("\n", "\\n")
        content = content.replace("\r", "\\r")
        content = content.replace("\t", "\\t")
        content = content.replace("\x00", "\\\\")  # 恢復反斜線
        return '"' + content + '"'

    fixed = _re.sub(r'"([^"]*?)"', fix_string_match, s)
    return fixed


def format_tool_history_block(
    tool_name: str,
    args: dict | None,
    result_raw: str,
    max_result_length: int = 2000,
) -> str:
    """
    產生 [START_PREV_ACTION] ... [END_PREV_ACTION] 區塊。

    格式:
        [START_PREV_ACTION]
        [ACTION_TYPE]
        <tool_name>
        [ACTION_ARG]
        <k:v pairs>
        [RESULT]
        <k:v pairs or null>
        [END_PREV_ACTION]

    這個格式刻意避免使用 { } 或 <tag> 標籤，防止模型模仿。
    """
    args_section = _format_args_flat(args, max_length=500)
    result_section = _format_result_flat(result_raw, max_length=max_result_length)

    block = (
        "[START_PREV_ACTION]\n"
        "[ACTION_TYPE]\n"
        f"{tool_name}\n"
        "[ACTION_ARG]\n"
        f"{args_section}\n"
        "[RESULT]\n"
        f"{result_section}\n"
        "[END_PREV_ACTION]"
    )

    return block


def _generate_natural_description(info: dict, seed: int = 0, index: int = 0) -> str:
    """
    Generate natural language description based on tool info.

    Core design principles:
    1. No fixed template format (avoid model mimicry)
    2. Multiple sentence style variations
    3. Describes like "context review in conversation" not "tool call record"
    4. Result part keeps full data for model to use
    5. **Deterministic random**: same seed + index produces same result for KV cache
    """
    tool_name = info["tool_name"]
    args_summary = info["args_summary"]
    result_summary = info["result_summary"]

    tool_type = _classify_tool(tool_name)

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

    rng = random.Random(seed + index)
    return rng.choice(styles)


def format_tool_history_legacy(info: dict, seed: int = 0, index: int = 0) -> str:
    """Legacy format: natural language description."""
    return _generate_natural_description(info, seed, index)


def _get_sanitization_config(config: dict) -> tuple:
    """Get sanitization config, return (enabled, max_result_length, format)."""
    enabled = config.get("enable_history_sanitization", True)
    max_length = config.get("sanitization_result_max_length", 2000)
    fmt = config.get("tool_history_format", "flat")
    return bool(enabled), int(max_length), str(fmt)


def _extract_tool_info(tag: str, max_result_length: int) -> dict:
    """
    Extract tool info from a <details> tag.

    Returns: {tool_name, args_summary, args_obj, result_summary, result_raw, truncated}
    """
    import re as _re
    import html as _html
    import json as _json

    name_match = _re.search(r'name=([^ >]+)', tag, flags=_re.IGNORECASE)
    tool_name = _html.unescape(name_match.group(1)) if name_match else "unknown"

    args_match = _re.search(r'<arguments>(.*?)</arguments>', tag, _re.DOTALL)
    args_summary = ""
    args_obj = None
    if args_match:
        args_raw = _html.unescape(args_match.group(1).strip())
        try:
            args_obj = _json.loads(args_raw)
            clean_args = {k: v for k, v in args_obj.items() if k not in ("tool_name", "label")}
            if clean_args:
                for k, v in clean_args.items():
                    if isinstance(v, str) and len(v) < 100:
                        args_summary = f"查詢「{v}」"
                        break
                    elif isinstance(v, (int, float, bool)):
                        args_summary = f"參數 {k}={v}"
                        break
                else:
                    args_summary = _json.dumps(clean_args, ensure_ascii=False)[:100]
        except _json.JSONDecodeError:
            args_summary = args_raw[:100]

    result_match = _re.search(r'<result>(.*?)</result>', tag, _re.DOTALL)
    result_summary = ""
    result_raw = ""
    truncated = False
    if result_match:
        result_raw = _html.unescape(result_match.group(1).strip())
        try:
            result_obj = _json.loads(result_raw)
            if isinstance(result_obj, dict):
                if "result" in result_obj and isinstance(result_obj["result"], str):
                    inner = result_obj["result"]
                    try:
                        inner_obj = _json.loads(inner)
                        result_summary = _json.dumps(inner_obj, ensure_ascii=False)
                    except _json.JSONDecodeError:
                        result_summary = inner
                elif "data" in result_obj:
                    result_summary = _json.dumps(result_obj["data"], ensure_ascii=False)
                elif "success" in result_obj:
                    result_summary = _json.dumps(result_obj, ensure_ascii=False)
                else:
                    result_summary = _json.dumps(result_obj, ensure_ascii=False)
            else:
                result_summary = str(result_obj)
        except _json.JSONDecodeError:
            result_summary = result_raw

        if len(result_summary) > max_result_length:
            result_summary = result_summary[:max_result_length] + "..."
            truncated = True

    return {
        "tool_name": tool_name,
        "args_summary": args_summary,
        "args_obj": args_obj,
        "result_summary": result_summary,
        "result_raw": result_raw,
        "truncated": truncated,
    }


def _classify_tool(tool_name: str) -> str:
    """Classify tool type by name."""
    search_tools = ["web_search", "brave_web_search", "search_files", "session_search"]
    trading_tools = ["coinglass", "mtf_analysis", "signal", "trading", "position", "order"]
    file_tools = ["read_file", "write_file", "patch", "search_files", "file"]
    code_tools = ["execute_code", "terminal", "process", "terminal_command"]

    tn = tool_name.lower()
    if any(s in tn for s in search_tools):
        return "search"
    if any(s in tn for s in trading_tools):
        return "trading"
    if any(s in tn for s in file_tools):
        return "file"
    if any(s in tn for s in code_tools):
        return "code"
    return "general"


def sanitize_message_content(content: str | None, seed: int = 0, max_result_length: int = 2000, fmt: str = "flat") -> tuple[str, int]:
    """
    Sanitize a single message's content by replacing <details> tags with safe format.

    Parameters:
        content: The message content
        seed: Random seed for deterministic style selection
        max_result_length: Max length for result summary
        fmt: Format type - "flat" or "legacy"

    Returns:
        (sanitized_content, replacement_count)
    """
    import re as _re

    if not content:
        return (content, 0)

    total_replacements = 0

    def _replace_details(match):
        nonlocal total_replacements
        total_replacements += 1
        idx = total_replacements
        tag = match.group(0)

        info = _extract_tool_info(tag, max_result_length)

        if fmt == "flat":
            return format_tool_history_block(
                tool_name=info["tool_name"],
                args=info["args_obj"],
                result_raw=info["result_raw"],
                max_result_length=max_result_length,
            )
        else:
            return format_tool_history_legacy(info, seed, idx)

    # 1. Standard <details type="tool_calls"> blocks (safe patterns, ReDoS-resistant)
    pattern1 = r'<details[^>]*type=tool_calls[^>]*>((?:(?!<details>).)*?)</details>'
    sanitized = _re.sub(pattern1, _replace_details, content, flags=_re.DOTALL | _re.IGNORECASE)

    # 2. <details> without type attribute (model-imitated format), with sub-tags
    # Use \s+ instead of \s*\n\s* since DOTALL makes . match newlines anyway
    pattern2 = r'<details[^>]*>\s+<summary>(?:(?!<summary>).)*?</summary>(?:(?!<arguments>).)*?<arguments>(?:(?!<arguments>).)*?</arguments>(?:(?!<result>).)*?<result>(?:(?!<result>).)*?</result>(?:(?!<details>).)*?</details>'
    sanitized = _re.sub(pattern2, _replace_details, sanitized, flags=_re.DOTALL | _re.IGNORECASE)

    # 3. Catch-all: any <details> containing <arguments> and <result>
    pattern3 = r'<details[^>]*>(?:(?!<details>).)*?<arguments>(?:(?!<arguments>).)*?</arguments>(?:(?!<result>).)*?<result>(?:(?!<result>).)*?</result>(?:(?!<details>).)*?</details>'
    sanitized = _re.sub(pattern3, _replace_details, sanitized, flags=_re.DOTALL | _re.IGNORECASE)

    return sanitized, total_replacements
