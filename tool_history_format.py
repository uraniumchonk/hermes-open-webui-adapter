"""
Tool History Format — shared utilities for history sanitization.

Only structured (OpenAI native tool role) format is supported.
Flat/legacy text-based formats have been removed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _get_sanitization_config(config: dict) -> tuple:
    """Get sanitization config, return (enabled, max_result_length, format).

    Runtime only implements structured (native tool role).
    flat/legacy remain documented in README / git ≤877fdb7.
    """
    enabled = config.get("enable_history_sanitization", True)
    max_length = config.get("sanitization_result_max_length", 20000)
    fmt = str(config.get("tool_history_format", "structured") or "structured").strip().lower()
    if fmt not in ("structured", "flat", "legacy"):
        logger.warning(
            "[history] unknown tool_history_format=%r — falling back to structured",
            fmt,
        )
        fmt = "structured"
    return bool(enabled), int(max_length), fmt


def _extract_tool_info(tag: str, max_result_length: int) -> dict:
    """
    Extract tool info from a <details> tag.

    Returns: {tool_name, args_summary, args_obj, result_summary, result_raw, truncated}
    """
    import html as _html

    # 防災難：模型模仿的 <details> tag 可能超大（無上限的 result）。
    # regex + html.unescape + json.loads 對巨大字串會吃爆記憶體。
    # 截斷後處理：保留前半段（name/arguments 通常在 tag 開頭），
    # result 部分截斷到上限即可，損失可接受。
    MAX_TAG_PROCESS_LEN = 512 * 1024  # 512KB
    if len(tag) > MAX_TAG_PROCESS_LEN:
        tag = tag[:MAX_TAG_PROCESS_LEN] + "</details>"

    name_match = re.search(r'name=([^ >]+)', tag, flags=re.IGNORECASE)
    tool_name = _html.unescape(name_match.group(1)) if name_match else "unknown"

    args_match = re.search(r'<arguments>(.*?)</arguments>', tag, re.DOTALL)
    args_summary = ""
    args_obj = None
    if args_match:
        args_raw = _html.unescape(args_match.group(1).strip())
        try:
            args_obj = json.loads(args_raw)
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
                    args_summary = json.dumps(clean_args, ensure_ascii=False)[:100]
        except json.JSONDecodeError:
            args_summary = args_raw[:100]

    result_match = re.search(r'<result>(.*?)</result>', tag, re.DOTALL)
    result_summary = ""
    result_raw = ""
    truncated = False
    if result_match:
        result_raw = _html.unescape(result_match.group(1).strip())
        try:
            result_obj = json.loads(result_raw)
            if isinstance(result_obj, dict):
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