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
import html
from typing import Any


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
