"""
responses_session — /v1/responses 路徑的 session marker 系統。

只針對 responses 路徑，不碰 chat/completions（後者的 marker mode 在
main.py 的 session_isolation，本模組與它完全獨立）。

機制（舊 session_isolation marker mode 的邏輯重寫，針對 Responses API）：

- Response 側：讀 gateway 回應 header 的 X-Hermes-Session-Id，把
  <!--hermes-sid:<id>--> HTML 註解 marker 嵌進 assistant 最終文字。
  OWUI 存下 assistant 訊息（註解在 markdown 渲染不可見；HTML 能存活
  OWUI 往返——<details> tool card 就是同一機制）。
- Request 側：從 input items 反向掃 assistant content 找 marker。
  - 找到 → session continuation：input 重寫成 [最新 user] 而已，
    帶 X-Hermes-Session-Id header，gateway 從 SessionDB 載入完整
    transcript（含所有工具呼叫與結果）。
  - 找不到 → 第一輪：原樣轉發，response 側會嵌入 marker。

為什麼用 gateway 自己的 session id（不是 filter 生成的 fingerprint）：
- 不需要 mapping 表、不會有兩個對話碰撞
- id 直接對應 SessionDB row
- 壓縮輪轉：最新的 marker 帶當前 live tip；gateway 的
  _resolve_live_session_id 也會把舊 id 解析到 live 端（雙重保險）

marker 永遠不會到模型眼前：filter 丟棄 payload 歷史、gateway 從 DB
載入（DB 是 canonical，乾淨無 marker）；DB 也不會被污染（gateway 先
persist，filter 之後才在 wire 上追加 marker）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncGenerator, Dict, List, Optional

logger = logging.getLogger("tool-filter")

# ── Marker 格式 ─────────────────────────────────────────────
# HTML 註解：OWUI markdown 渲染不可見、存檔存活（同 <details> 先例）。
# id 字元集：uuid4 或 api-<16hex>（gateway 兩種 session id 格式都涵蓋）。
SID_MARKER_RE = re.compile(r"<!--hermes-sid:([A-Za-z0-9][A-Za-z0-9_-]{7,63})-->")


def build_marker(session_id: str) -> str:
    """Build the sid marker for embedding into assistant content."""
    return f"<!--hermes-sid:{session_id}-->"


def strip_markers(text: Any) -> Any:
    """Remove sid markers from a text value (str passthrough for non-str)."""
    if not isinstance(text, str) or "<!--hermes-sid:" not in text:
        return text
    return SID_MARKER_RE.sub("", text)


def _item_texts(item: Dict[str, Any]) -> List[str]:
    """Extract text payloads from a Responses input item (str or parts list).

    Accepts ANY part type carrying a "text" key — the Responses API uses
    "output_text" for assistant parts and "input_text" for user parts
    (Open WebUI's convert_to_responses_payload emits exactly these), and
    plain "text" for compatibility clients. Filtering on a single type
    here was the OWUI tool-amnesia leak: the marker went undetected, the
    full body was forwarded, and the LLM saw (and hallucinated copies of)
    the marker.
    """
    content = item.get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        ]
    return []


def extract_session_id(input_items: Any) -> Optional[str]:
    """Scan input items in REVERSE for the sid marker in assistant content.

    Returns the last marker found — the latest assistant message carries the
    current session tip (compression rotation changes the id mid-conversation;
    older markers are stale, and the gateway's _resolve_live_session_id also
    resolves them, so either way the latest is the right choice).

    Only assistant items are scanned: a marker in a user item means the user
    pasted an assistant message — not a continuation signal.
    """
    if not isinstance(input_items, list):
        return None
    for item in reversed(input_items):
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        for text in _item_texts(item):
            matches = SID_MARKER_RE.findall(text)
            if matches:
                return matches[-1]
    return None


def rewrite_input_to_last_user(req_json: Dict[str, Any]) -> bool:
    """Rewrite ``input`` to [last user item] only (markers stripped).

    The gateway loads the full history from SessionDB via
    X-Hermes-Session-Id, so the body history is redundant — dropping it is
    what makes the request light. Returns True when rewritten.
    """
    input_items = req_json.get("input")
    if not isinstance(input_items, list):
        return False
    last_user: Optional[Dict[str, Any]] = None
    for item in reversed(input_items):
        if isinstance(item, dict) and item.get("role") == "user":
            last_user = item
            break
    if last_user is None:
        return False
    content = last_user.get("content")
    if isinstance(content, str):
        content = strip_markers(content)
    elif isinstance(content, list):
        content = [
            {**p, "text": strip_markers(p["text"])}
            if isinstance(p, dict) and isinstance(p.get("text"), str)
            else p
            for p in content
        ]
    req_json["input"] = [{**last_user, "content": content}]
    return True


# ── Response 側：marker 注入 ─────────────────────────────────


def inject_marker_into_blocking_response(resp_json: Dict[str, Any], session_id: str) -> bool:
    """Append the marker to the final message item of a non-streaming
    Responses response (the ``output`` array). Returns True on injection."""
    output = resp_json.get("output")
    if not isinstance(output, list):
        return False
    marker = build_marker(session_id)
    for item in reversed(output):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if isinstance(content, list):
            for part in reversed(content):
                if isinstance(part, dict) and part.get("type") == "output_text":
                    part["text"] = str(part.get("text", "")) + marker
                    return True
        # message item without content parts: nothing to append to
        return False
    return False


def _append_marker_to_frame(frame_bytes: bytes, marker: str) -> bytes:
    """Append the marker to the text field of a delta / done / completed frame.

    Returns the original frame unchanged when the frame is not one of those
    events, carries no text, or fails to parse (passthrough safety first).
    """
    try:
        lines = frame_bytes.decode("utf-8", errors="replace").strip().split("\n")
        event_type: Optional[str] = None
        data_lines: List[str] = []
        for line in lines:
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        if not data_lines:
            return frame_bytes
        data = json.loads("\n".join(data_lines))
        modified = False
        if event_type == "response.output_text.delta":
            if isinstance(data.get("delta"), str) and data["delta"]:
                data["delta"] = data["delta"] + marker
                modified = True
        elif event_type == "response.output_text.done":
            if isinstance(data.get("text"), str):
                data["text"] = data["text"] + marker
                modified = True
        elif event_type == "response.completed":
            resp_obj = data.get("response", {})
            for item in reversed(resp_obj.get("output", [])):
                if isinstance(item, dict) and item.get("type") == "message":
                    for part in reversed(item.get("content", [])):
                        if (
                            isinstance(part, dict)
                            and part.get("type") == "output_text"
                            and isinstance(part.get("text"), str)
                        ):
                            part["text"] = part["text"] + marker
                            modified = True
                            break
                    if modified:
                        break
        if not modified:
            return frame_bytes
        # Rebuild the frame: keep non-data lines (event: / comments), replace data.
        out_lines: List[str] = []
        for line in lines:
            if line.startswith("data:"):
                break
            out_lines.append(line)
        out_lines.append("data: " + json.dumps(data, ensure_ascii=False))
        return "\n".join(out_lines).encode("utf-8")
    except Exception:
        return frame_bytes


async def stream_with_marker(
    resp, session_id: str
) -> AsyncGenerator[bytes, None]:
    """Passthrough the Responses SSE stream, appending the sid marker to the
    assistant's final text.

    The marker lands in three places (whichever source OWUI stores from):
    - the LAST response.output_text.delta (buffered one frame ahead)
    - the response.output_text.done text field (full text)
    - the response.completed envelope's final message item

    Duplicate markers are harmless: extraction takes the last match, and the
    marker never reaches the model (the filter drops the payload history; the
    gateway loads from SessionDB).
    """
    marker = build_marker(session_id)
    buffer = b""
    pending_delta: Optional[bytes] = None
    placed = 0

    def _flush_pending() -> Optional[bytes]:
        nonlocal pending_delta, placed
        if pending_delta is None:
            return None
        frame = _append_marker_to_frame(pending_delta, marker)
        if frame is not pending_delta:
            placed += 1
        pending_delta = None
        return frame + b"\n\n"

    while True:
        try:
            line = await resp.content.readline()
        except Exception as e:
            logger.error(f"[responses-session] stream read error: {type(e).__name__}: {e}")
            raise
        if not line:
            break
        buffer += line
        while b"\n\n" in buffer:
            frame_bytes, buffer = buffer.split(b"\n\n", 1)
            frame = frame_bytes.decode("utf-8", errors="replace")
            event_type = None
            for line_item in frame.split("\n"):
                if line_item.startswith("event: "):
                    event_type = line_item[7:].strip()
                    break
            if event_type == "response.output_text.delta":
                # Buffer: this may be the last delta; hold it until the next
                # frame (or EOF) decides.
                if pending_delta is not None:
                    yield pending_delta + b"\n\n"
                pending_delta = frame_bytes
                continue
            # Non-delta frame: flush the buffered delta (with marker if it was
            # the last one), then handle this frame.
            flushed = _flush_pending()
            if flushed is not None:
                yield flushed
            if event_type in ("response.output_text.done", "response.completed"):
                out = _append_marker_to_frame(frame_bytes, marker)
                if out is not frame_bytes:
                    placed += 1
                yield out + b"\n\n"
                continue
            yield frame_bytes + b"\n\n"
    # EOF: flush whatever is left.
    flushed = _flush_pending()
    if flushed is not None:
        yield flushed
    if buffer.strip():
        yield buffer.rstrip(b"\r\n") + b"\n\n"
    if placed:
        logger.info(f"[responses-session] Marker embedded in stream: sid={session_id[:12]}… ({placed} frame(s))")
    else:
        logger.warning(f"[responses-session] No frame carried the marker: sid={session_id[:12]}… (empty final text?)")
