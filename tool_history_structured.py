"""
tool_history_structured — 將 Open WebUI 的 <details type="tool_calls">
歷史轉成 OpenAI 原生 assistant.tool_calls + role=tool messages。

目標：從結構層消滅上下文污染，讓 chat_template 渲染出 <|im_start|>tool
special token，模型天生理解這是歷史而非該模仿的輸出格式。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from uuid import uuid4

from tool_history_format import _extract_tool_info, _get_sanitization_config

logger = logging.getLogger(__name__)

# 與舊 sanitize 對齊的三種 details 模式（type=tool_calls / 仿冒 tag / catch-all）
# 注意：原始 pattern1 為 `type=tool_calls`（無引號），對 Open WebUI 實際輸出
# `type="tool_calls"`（帶引號）不匹配，會落到 pattern2/3（兩者都要求 <arguments>）。
# 這裡放寬為「可選引號 + 可選空白」，讓帶引號 type 且缺少 <arguments> 的區塊
# 也能被正確轉換（arguments 回退為 {}）。對原本 pattern1 能匹配的輸入行為不變。
_PATTERN_TYPE_TOOL_CALLS = re.compile(
    r"<details[^>]*type\s*=\s*[\"']?tool_calls[\"']?[^>]*>((?:(?!<details>).)*?)</details>",
    re.DOTALL | re.IGNORECASE,
)
_PATTERN_WITH_SUBTAGS = re.compile(
    r"<details[^>]*>\s+<summary>(?:(?!<summary>).)*?</summary>"
    r"(?:(?!<arguments>).)*?<arguments>(?:(?!<arguments>).)*?</arguments>"
    r"(?:(?!<result>).)*?<result>(?:(?!<result>).)*?</result>"
    r"(?:(?!<details>).)*?</details>",
    re.DOTALL | re.IGNORECASE,
)
_PATTERN_CATCHALL = re.compile(
    r"<details[^>]*>(?:(?!<details>).)*?<arguments>(?:(?!<arguments>).)*?</arguments>"
    r"(?:(?!<result>).)*?<result>(?:(?!<result>).)*?</result>"
    r"(?:(?!<details>).)*?</details>",
    re.DOTALL | re.IGNORECASE,
)

_ALL_PATTERNS = (_PATTERN_TYPE_TOOL_CALLS, _PATTERN_WITH_SUBTAGS, _PATTERN_CATCHALL)


def _clean_tool_name(name: str) -> str:
    """去掉 HTML 屬性值上的引號與空白。"""
    if not name:
        return "unknown"
    return name.strip().strip("\"'").strip() or "unknown"


def _find_details_spans(content: str) -> list[tuple[int, int, str]]:
    """
    在 content 中找出所有 tool-call <details> 區塊的 (start, end, tag)。
    多 pattern 合併後依 start 排序，重疊時保留先出現（較具體）的 match。
    """
    candidates: list[tuple[int, int, str]] = []
    for pat in _ALL_PATTERNS:
        for m in pat.finditer(content):
            candidates.append((m.start(), m.end(), m.group(0)))

    if not candidates:
        return []

    candidates.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    selected: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, tag in candidates:
        if start < last_end:
            continue  # 與已選區間重疊，丟棄
        selected.append((start, end, tag))
        last_end = end
    return selected


def parse_assistant_content(
    content: str | None, max_result_length: int = 20000
) -> list[dict]:
    """
    把 assistant content 解析成有序 segments。

    Returns:
        list of {"type": "text", "text": str} | {"type": "tool", "info": dict}
        純空白 / 空 content → []
    """
    if not content:
        return []

    # 快速路徑
    if "<details" not in content:
        if content.strip():
            return [{"type": "text", "text": content}]
        return []

    spans = _find_details_spans(content)
    if not spans:
        return [{"type": "text", "text": content}]

    segments: list[dict] = []
    cursor = 0
    for start, end, tag in spans:
        if start > cursor:
            text = content[cursor:start]
            if text.strip():
                segments.append({"type": "text", "text": text})
        info = _extract_tool_info(tag, max_result_length)
        info["tool_name"] = _clean_tool_name(info.get("tool_name", "unknown"))
        segments.append({"type": "tool", "info": info})
        cursor = end

    if cursor < len(content):
        text = content[cursor:]
        if text.strip():
            segments.append({"type": "text", "text": text})

    return segments


def _args_to_json_string(args_obj: Any) -> str:
    if args_obj is None:
        return "{}"
    if isinstance(args_obj, str):
        # 已是字串：嘗試驗證 JSON，失敗則包成空物件
        try:
            json.loads(args_obj)
            return args_obj
        except (json.JSONDecodeError, TypeError):
            return "{}"
    try:
        # 去掉 Open WebUI 內部欄位
        if isinstance(args_obj, dict):
            clean = {k: v for k, v in args_obj.items() if k not in ("tool_name", "label")}
            return json.dumps(clean, ensure_ascii=False)
        return json.dumps(args_obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def _tool_result_content(info: dict) -> str:
    summary = info.get("result_summary") or ""
    if summary:
        return summary
    return info.get("result_raw") or ""


def segments_to_messages(
    segments: list[dict], call_id_prefix: str = "call_htf"
) -> list[dict]:
    """
    把 segments 轉成 OpenAI chat messages。

    每個 tool 各自配一對 assistant(tool_calls) → tool，
    以符合「tool message 必須緊接宣告它的 assistant」的契約。
    """
    if not segments:
        return []

    messages: list[dict] = []
    pending_text: Optional[str] = None
    used_ids: set[str] = set()

    def _new_call_id() -> str:
        for _ in range(8):
            cid = f"{call_id_prefix}_{uuid4().hex[:8]}"
            if cid not in used_ids:
                used_ids.add(cid)
                return cid
        # 極低機率 fallback
        cid = f"{call_id_prefix}_{uuid4().hex}"
        used_ids.add(cid)
        return cid

    def _flush_trailing_text() -> None:
        nonlocal pending_text
        if pending_text is not None and pending_text.strip():
            messages.append({"role": "assistant", "content": pending_text})
        pending_text = None

    i = 0
    n = len(segments)
    while i < n:
        seg = segments[i]
        if seg["type"] == "text":
            # 累積文字；若後面沒有 tool，最後 flush 成純 assistant
            text = seg.get("text") or ""
            if pending_text is None:
                pending_text = text
            else:
                pending_text = pending_text + text
            i += 1
            continue

        # tool segment
        info = seg.get("info") or {}
        call_id = _new_call_id()
        name = _clean_tool_name(info.get("tool_name", "unknown"))
        args_str = _args_to_json_string(info.get("args_obj"))

        content = ""
        if pending_text is not None:
            # 保留原始文字（僅「純空白」時才視為無文字）；規格允許 "" 或 null
            content = pending_text if pending_text.strip() else ""
            pending_text = None

        messages.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": args_str,
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": _tool_result_content(info),
            }
        )
        i += 1

    _flush_trailing_text()
    return messages


def convert_assistant_message(
    msg: dict, max_result_length: int = 20000
) -> list[dict]:
    """
    轉換單條 assistant message。
    - 已有 tool_calls → 原樣
    - content 無 details → 原樣
    - 否則拆成多條
    """
    if not isinstance(msg, dict):
        return [msg]

    if "tool_calls" in msg:
        return [msg]

    content = msg.get("content")
    if not content or not isinstance(content, str):
        return [msg]

    if "<details" not in content:
        return [msg]

    spans = _find_details_spans(content)
    if not spans:
        return [msg]

    segments = parse_assistant_content(content, max_result_length=max_result_length)
    if not any(s.get("type") == "tool" for s in segments):
        return [msg]

    return segments_to_messages(segments)


def sanitize_messages_structured(
    messages: list, config: Optional[dict] = None
) -> list:
    """
    主入口：遍歷 messages，對含 <details> 的 assistant 做結構化拆分。
    回傳新 list；非 assistant 原樣透傳。
    """
    if not messages:
        return messages

    config = config or {}
    _, max_length, _ = _get_sanitization_config(config)
    if "sanitization_result_max_length" not in config:
        # API 規格：預設 20000（_get_sanitization_config 的預設是 2000）
        max_length = 20000

    out: list = []
    tools_converted = 0
    messages_expanded = 0

    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            out.append(msg)
            continue

        converted = convert_assistant_message(msg, max_result_length=max_length)
        if len(converted) != 1 or converted[0] is not msg:
            messages_expanded += 1
            tools_converted += sum(1 for m in converted if m.get("role") == "tool")
        out.extend(converted)

    if tools_converted > 0:
        logger.info(
            "[sanitization:structured] Converted %s tool result(s) across %s "
            "assistant message(s); output messages=%s (input=%s)",
            tools_converted,
            messages_expanded,
            len(out),
            len(messages),
        )

    return out
