"""
special_tags — 特殊標籤註冊表與 neutralize / strip 函式。

兩個任務：
- Task 1（request path）：strip_thinking_content
    從 assistant messages 移除思考內容（reasoning 欄位 + THINK_TAG 區塊），
    避免 OWUI 把思考區域組裝回傳給 LLM（污染反饋迴圈）。
- Task 2（response path）：neutralize_special_tags
    把 tool result body 內的特殊標籤 neutralize（HTML-escape < 或插入 zero-width space），
    避免 OWUI tag 偵測通拉跳脫（檔案內容含 THINK_TAG / TOOL_TAG 等）。

實際標籤字串在 special_tags.json（見 extract_tags.py）。
本模組 import 時載入；JSON 不存在時用程式化 fallback（不在 code 直接寫實際字串）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 標籤載入 ────────────────────────────────────────────────
_SPECIAL_TAGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "special_tags.json")


def _load_data() -> Dict[str, Any]:
    try:
        with open(_SPECIAL_TAGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(
            f"[special-tags] Failed to load {_SPECIAL_TAGS_FILE}: {e}; using fallback"
        )
        return {}


_DATA = _load_data()


# ── Fallback（程式化建構，避免在 code 直接寫實際標籤字串）────
def _t(name: str) -> str:
    """開標籤：<name>"""
    return "<" + name + ">"


def _ct(name: str) -> str:
    """關標籤：</name>"""
    return "<" + "/" + name + ">"


def _fallback_neutralize_tags() -> List[str]:
    return [
        _t("tools"), _ct("tools"),
        _t("tool_call"), _ct("tool_call"),
        _t("think"), _ct("think"),
        _t("thinking"), _ct("thinking"),
        _t("reason"), _ct("reason"),
        _t("reasoning"), _ct("reasoning"),
        _t("thought"), _ct("thought"),
        _t("Thought"), _ct("Thought"),
        "<" + "|begin_of_thought|>", "<" + "|end_of_thought|>",
        "◁" + "think" + "▷", "◁" + "/" + "think" + "▷",
    ]


def _fallback_thinking_pairs() -> List[tuple]:
    return [
        (_t("think"), _ct("think")),
        (_t("thinking"), _ct("thinking")),
        (_t("reason"), _ct("reason")),
        (_t("reasoning"), _ct("reasoning")),
        (_t("thought"), _ct("thought")),
        (_t("Thought"), _ct("Thought")),
        ("<" + "|begin_of_thought|>", "<" + "|end_of_thought|>"),
        ("◁" + "think" + "▷", "◁" + "/" + "think" + "▷"),
    ]


def _get_neutralize_tags() -> List[str]:
    """要 neutralize 的標籤清單。排除 <details（由 main._neutralize_details_tags 處理）。"""
    tags = _DATA.get("all_tags_to_neutralize")
    if not tags:
        tags = _fallback_neutralize_tags()
    return [t for t in tags if t not in ("<details", "</details>")]


def _get_thinking_pairs() -> List[tuple]:
    """思考標籤對 (open, close)。模型 THINK_TAG + OWUI reasoning tags。"""
    pairs = _DATA.get("owui_reasoning_tags")
    if pairs:
        jinja_tags = _DATA.get("model_jinja_tags", [])
        result: List[tuple] = []
        if _t("think") in jinja_tags and _ct("think") in jinja_tags:
            result.append((_t("think"), _ct("think")))
        result.extend(pairs)
        return result
    return _fallback_thinking_pairs()


NEUTRALIZE_TAGS: List[str] = _get_neutralize_tags()
THINKING_PAIRS: List[tuple] = _get_thinking_pairs()

if _DATA:
    logger.info(
        f"[special-tags] Loaded from JSON: {len(NEUTRALIZE_TAGS)} neutralize tags, "
        f"{len(THINKING_PAIRS)} thinking pairs"
    )
else:
    logger.info(
        f"[special-tags] Using fallback: {len(NEUTRALIZE_TAGS)} neutralize tags, "
        f"{len(THINKING_PAIRS)} thinking pairs"
    )


# ── Task 2：neutralize 特殊標籤（response path）─────────────
def neutralize_special_tags(text: str, tags: Optional[List[str]] = None) -> str:
    """
    把 body 內的特殊標籤 neutralize，避免 OWUI tag 偵測通拉跳脫。

    OWUI tag_output_handler 掃原始 content stream（含 code block / 純文字），
    所以標籤要實際改字串打破匹配：
    - < 開頭完整標籤（含 >）：regex 匹配（允許 attributes），< 換 &lt;
    - < 開頭前綴（不含 >）：< 換 &lt;
    - 非 < 開頭（如 ◁think▷）：插入 zero-width space 打破 exact match

    OWUI 渲染時 decode &lt; 還原成 <，code fence 顯示仍是乾淨原文。
    """
    if not text:
        return text
    tags = tags or NEUTRALIZE_TAGS
    for tag in tags:
        if not tag:
            continue
        if tag.startswith("<") and tag.endswith(">"):
            inner = tag[1:-1]
            try:
                pattern = re.compile(rf"<{re.escape(inner)}(?:\s[^>]*)?>")
                text = pattern.sub(lambda m: "&lt;" + m.group(0)[1:], text)
            except re.error:
                text = text.replace(tag, "&lt;" + tag[1:])
        elif tag.startswith("<"):
            text = text.replace(tag, "&lt;" + tag[1:])
        else:
            text = text.replace(tag, tag[0] + "\u200b" + tag[1:])
    return text


# ── Task 1：strip 思考內容（request path）──────────────────
def strip_thinking_content(messages: list) -> list:
    """
    從 assistant messages 移除思考內容（request path）。

    - 移除 reasoning / reasoning_content / thinking 欄位
    - 從 content 移除 THINK_TAG 區塊（含內容）

    目的：OWUI 把思考區域組裝回傳給 LLM 時砍掉思考區域，
    避免模型看到舊思考（污染反饋迴圈）。
    回傳新 list；非 assistant 原樣透傳。
    """
    if not messages:
        return messages

    out: list = []
    stripped_fields = 0
    stripped_blocks = 0

    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        if msg.get("role") != "assistant":
            out.append(msg)
            continue

        new_msg = dict(msg)

        # 移除思考欄位
        for field in ("reasoning", "reasoning_content", "thinking"):
            if field in new_msg:
                del new_msg[field]
                stripped_fields += 1

        # 從 content 移除 THINK_TAG 區塊（含內容）
        content = new_msg.get("content")
        if isinstance(content, str) and content:
            for open_str, close_str in THINKING_PAIRS:
                pattern = re.compile(
                    re.escape(open_str) + r".*?" + re.escape(close_str),
                    re.DOTALL,
                )
                content, n = pattern.subn("", content)
                if n > 0:
                    stripped_blocks += n
            new_msg["content"] = content

        out.append(new_msg)

    if stripped_fields > 0 or stripped_blocks > 0:
        logger.info(
            f"[thinking-strip] Removed {stripped_fields} thinking field(s), "
            f"{stripped_blocks} thinking block(s) from assistant messages"
        )

    return out
