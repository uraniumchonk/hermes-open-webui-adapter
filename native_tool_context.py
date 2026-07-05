"""
Native Tool Context System — 核心模組

實現四個組件：
1. 完全透傳 Filter (Native Passthrough Mode)
2. Tool Event 捕獲與簡短通知
3. Persistent Tool Context Cache (SQLite)
4. Session Marker 檢測與歷史注入

設計理念：
- 不修改任何 SSE data（組件1）
- 捕獲 hermes.tool.progress 事件中的完整 tool result（組件2+3）
- 使用 SQLite 持久化存儲（組件3）
- 在 handle_completions_request 中攔截 request body 並注入完整 results（組件4）
"""

import aiosqlite
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("tool-filter")

# ── SQLite Database Schema ──────────────────────────────────

SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS tool_context (
        session_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        tool_call_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        arguments TEXT,
        result TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (session_id, message_id, tool_call_id)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_tool_context_session ON tool_context(session_id)""",
    """CREATE INDEX IF NOT EXISTS idx_tool_context_session_message ON tool_context(session_id, message_id)""",
]

# ── Session Marker Pattern ──────────────────────────────────

# 現有格式：```session\n{session_id}  {timestamp}\n```
SESSION_MARKER_PATTERN = re.compile(
    r"```session\s*\n\s*(api-[a-f0-9]{16})\s+(\d{4}-\d{2}-\d{2}T[^\s]+)\s*\n```",
    re.IGNORECASE
)

# ── Database Manager ────────────────────────────────────────

class ToolContextDatabase:
    """SQLite 持久化 Tool Context Cache"""
    
    def __init__(self, db_path: str = "tool_context.db"):
        self.db_path = Path(db_path)
        self._db: Optional[aiosqlite.Connection] = None
        self.ttl_seconds = 86400  # 24 hours default
    
    async def connect(self):
        if self._db is None:
            self._db = await aiosqlite.connect(str(self.db_path))
            for sql in SCHEMA_SQL:
                await self._db.execute(sql)
            await self._db.commit()
            logger.info(f"[tool-context] Connected to SQLite at {self.db_path}")
    
    async def close(self):
        if self._db:
            await self._db.close()
            self._db = None
            logger.info(f"[tool-context] Closed SQLite connection")
    
    async def store_tool_result(
        self,
        session_id: str,
        message_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: Optional[Dict] = None,
        result: Optional[str] = None,
    ):
        """Store a complete tool result in the database."""
        await self.connect()
        args_json = json.dumps(arguments, ensure_ascii=False) if arguments else None
        await self._db.execute(
            """INSERT OR REPLACE INTO tool_context 
               (session_id, message_id, tool_call_id, tool_name, arguments, result, created_at)
               VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (session_id, message_id, tool_call_id, tool_name, args_json, result)
        )
        await self._db.commit()
        logger.debug(
            f"[tool-context] Stored: session={session_id[:8]}... message={message_id[:8]}... "
            f"tool={tool_name} tc_id={tool_call_id[:8]}..."
        )
    
    async def get_tool_results_by_session(
        self, session_id: str, message_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Retrieve all tool results for a session (optionally filtered by message_id)."""
        await self.connect()
        if message_id:
            cursor = await self._db.execute(
                "SELECT tool_call_id, tool_name, arguments, result FROM tool_context "
                "WHERE session_id = ? AND message_id = ? ORDER BY created_at",
                (session_id, message_id)
            )
        else:
            cursor = await self._db.execute(
                "SELECT tool_call_id, tool_name, arguments, result FROM tool_context "
                "WHERE session_id = ? ORDER BY created_at",
                (session_id,)
            )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            tc_id, tool_name, args_json, res = row
            args = json.loads(args_json) if args_json else None
            result.append({
                "tool_call_id": tc_id,
                "tool_name": tool_name,
                "arguments": args,
                "result": res,
            })
        return result
    
    async def cleanup_by_ttl(self, ttl_seconds: Optional[int] = None):
        """Remove entries older than TTL."""
        ttl = ttl_seconds or self.ttl_seconds
        await self.connect()
        await self._db.execute(
            "DELETE FROM tool_context WHERE created_at < datetime('now', ?)",
            (f"-{ttl} seconds",)
        )
        count = self._db.changes
        await self._db.commit()
        if count > 0:
            logger.info(f"[tool-context] Cleaned up {count} stale entries (TTL={ttl}s)")


# ── Global Instance ─────────────────────────────────────────

_tool_context_db: Optional[ToolContextDatabase] = None


def get_tool_context_db(db_path: str = "tool_context.db") -> ToolContextDatabase:
    """Get or create the global ToolContextDatabase instance."""
    global _tool_context_db
    if _tool_context_db is None:
        _tool_context_db = ToolContextDatabase(db_path)
    return _tool_context_db


# ── Component 2: Tool Event Capture & Short Notification ────

def build_short_notification(status: str, tool_name: str) -> str:
    """
    Build a short notification for tool events.
    - running → "執行 terminal"
    - completed → "已完成 terminal"
    """
    if status == "running":
        return f"執行 {tool_name}"
    elif status == "completed":
        return f"已完成 {tool_name}"
    return ""


# ── Component 4: Session Marker Detection & History Injection ──

def detect_session_marker(messages: List[Dict]) -> Optional[Tuple[str, str]]:
    """
    Detect session marker in messages.
    Returns (session_id, timestamp) if found, None otherwise.
    """
    if not messages:
        return None
    
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            m = SESSION_MARKER_PATTERN.search(content)
            if m:
                return (m.group(1), m.group(2))
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    m = SESSION_MARKER_PATTERN.search(part["text"])
                    if m:
                        return (m.group(1), m.group(2))
    return None


def inject_tool_results_into_history(
    messages: List[Dict],
    session_id: str,
    tool_results: List[Dict[str, Any]],
) -> List[Dict]:
    """
    Inject complete tool results into the history messages.
    
    Strategy:
    - Find the last user message
    - Insert a system message with all tool results before it
    - Format as structured text with tool names, args, and results
    
    This is injected BEFORE the request is forwarded to Hermes Gateway,
    so the model sees full context.
    """
    if not tool_results:
        return messages
    
    # Find the last user message index
    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break
    
    if last_user_idx is None:
        return messages
    
    # Build a structured text block with all tool results
    parts = ["[TOOL_CONTEXT_INJECTED] Previous tool execution results:\n"]
    for i, tr in enumerate(tool_results, 1):
        tool_name = tr.get("tool_name", "unknown")
        tc_id = tr.get("tool_call_id", "")
        args = tr.get("arguments")
        result = tr.get("result")
        
        parts.append(f"\n--- Tool Call {i} ---")
        parts.append(f"Tool: {tool_name}")
        parts.append(f"ID: {tc_id}")
        if args:
            parts.append(f"Args: {json.dumps(args, ensure_ascii=False)}")
        if result:
            # Truncate if too long
            if len(result) > 5000:
                parts.append(f"Result: {result[:5000]}... (truncated)")
            else:
                parts.append(f"Result: {result}")
        parts.append("")
    
    injection_text = "\n".join(parts)
    
    # Insert as a system message before the last user message
    injection_msg = {
        "role": "system",
        "content": injection_text,
    }
    
    new_messages = list(messages)
    new_messages.insert(last_user_idx, injection_msg)
    
    logger.info(
        f"[tool-context] Injected {len(tool_results)} tool result(s) "
        f"into history for session={session_id[:8]}... at index {last_user_idx}"
    )
    
    return new_messages


# ── Component 1 + 2: Native Passthrough Transform Stream ────

async def native_passthrough_transform_stream(
    reader,
    model: str,
    completion_id: str,
    created: int,
    upstream_port: str,
    hermes_sid: str = "",
    db: Optional[ToolContextDatabase] = None,
    capture_notifications: bool = True,
):
    """
    組件1 + 2: 完全透傳 + Tool Event 捕獲
    
    核心行為：
    1. 透傳所有 SSE data chunks（包括 hermes.tool.progress）
    2. 捕獲 hermes.tool.progress 事件中的完整 tool result
    3. 可選：在 delta.content 中注入簡短通知
    4. 存儲到 SQLite（組件3）
    """
    import aiohttp
    
    # Track active tools for message_id derivation
    active_tools: Dict[str, dict] = {}
    done_received = False
    
    # Initial connection packets (from main.py)
    yield b': initial-connection-established\n\n'
    yield b'data: {"id":"%s","object":"chat.completion.chunk","created":%d,"model":"%s","choices":[{"index":0,"delta":{},"finish_reason":null}]}  \n\n' % (
        completion_id.encode(), created, model.encode()
    )
    
    buffer = b""
    last_heartbeat = time.monotonic()
    heartbeat_interval = 1.5
    first_content_sent = False
    
    while True:
        # Heartbeat
        elapsed = time.monotonic() - last_heartbeat
        if elapsed >= heartbeat_interval:
            if not first_content_sent:
                yield b': keepalive-waiting-first-chunk\n\n'
            else:
                yield b': keepalive\n\n'
            last_heartbeat = time.monotonic()
        
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.error(f"[native-passthrough] readline error: {e}")
            raise
        
        if not line:
            logger.info(f"[native-passthrough] EOF detected, done_received={done_received}")
            break
        
        buffer += line
        
        while b"\n\n" in buffer:
            frame_bytes, buffer = buffer.split(b"\n\n", 1)
            frame = frame_bytes.decode("utf-8", errors="replace")
            
            # Check for [DONE]
            if "[DONE]" in frame and not done_received:
                done_received = True
                yield (frame + "\n\n").encode("utf-8")
                continue
            
            # Parse SSE frame
            lines = frame.strip().split("\n")
            event_type = None
            data_lines = []
            
            for line_item in lines:
                if line_item.startswith("event: "):
                    event_type = line_item[7:].strip()
                elif line_item.startswith("data:") or line_item == "data:":
                    data_lines.append(line_item[5:].lstrip(" "))
            
            data_str = "\n".join(data_lines) if data_lines else None
            
            # Handle hermes.tool.progress events (Component 2 + 3)
            if event_type == "hermes.tool.progress" and data_str:
                try:
                    parsed = json.loads(data_str)
                    tc_id = parsed.get("toolCallId", "")
                    status = parsed.get("status", "")
                    tool = parsed.get("tool", "unknown")
                    arguments = parsed.get("arguments", {})
                    result = parsed.get("result", "")
                    
                    if status == "running":
                        active_tools[tc_id] = {
                            "tool": tool,
                            "arguments": arguments,
                            "status": "running",
                        }
                        # Component 2: Optional short notification
                        if capture_notifications:
                            notif = build_short_notification("running", tool)
                            if notif:
                                notif_chunk = _build_content_chunk(notif)
                                yield notif_chunk
                    
                    elif status == "completed":
                        # Component 3: Store to SQLite
                        if db and hermes_sid:
                            # Derive message_id from tc_id or use a default
                            message_id = tc_id[:16] if tc_id else "unknown"
                            await db.store_tool_result(
                                session_id=hermes_sid,
                                message_id=message_id,
                                tool_call_id=tc_id,
                                tool_name=tool,
                                arguments=arguments if isinstance(arguments, dict) else {},
                                result=result,
                            )
                        
                        # Component 2: Optional short notification
                        if capture_notifications:
                            notif = build_short_notification("completed", tool)
                            if notif:
                                notif_chunk = _build_content_chunk(notif)
                                yield notif_chunk
                        
                        # Remove from active tools
                        active_tools.pop(tc_id, None)
                    
                    # Component 1: DO NOT yield hermes.tool.progress to client
                    # Skip this frame
                    continue
                    
                except json.JSONDecodeError:
                    pass
            
            # For all other frames: passthrough as-is (Component 1)
            # Track first content sent
            if not first_content_sent and data_str:
                try:
                    pj = json.loads(data_str)
                    delta = pj.get("choices", [{}])[0].get("delta", {})
                    if delta.get("content"):
                        first_content_sent = True
                except (json.JSONDecodeError, IndexError, KeyError):
                    pass
            
            yield (frame + "\n\n").encode("utf-8")
    
    # Flush remaining buffer
    if buffer.strip():
        cleaned = buffer.decode("utf-8", errors="replace").rstrip("\r\n")
        if cleaned:
            yield (cleaned + "\n\n").encode("utf-8")


def _build_content_chunk(content: str) -> bytes:
    """Build an SSE data: line with delta.content."""
    payload = {"choices": [{"delta": {"content": content}}]}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


# ── Async import (needed for native_passthrough_transform_stream) ──

import asyncio