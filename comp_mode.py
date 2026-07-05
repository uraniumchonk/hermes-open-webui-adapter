"""
Client-side conversation compression via [comp] trigger.

When the user includes [comp] in their message, compress all tool results
in the conversation history before that message to reduce context window size.
"""

import logging

logger = logging.getLogger(__name__)

_COMP_TRIGGER = "[comp]"
_COMP_MARKER_CODE = "comp"
_COMP_NOTIFICATION = """⚠️ [CONVERSATION COMPRESSED] Previous tool results have been truncated to save context space. The full results are still available in the server-side session history. If you need to reference specific data from earlier tool calls, please re-run the relevant tools to reload the data into context."""


def _comp_mode_enabled(config: dict) -> bool:
    """Check if client-side compression mode is enabled."""
    # Support both "enabled" and "true" for backward compatibility
    val = config.get("comp_mode", "disabled")
    return val in ("enabled", "true")


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
    
    Keeps the tool name and args, but truncates the [RESULT] section.
    Returns (compressed_content, blocks_compressed).
    """
    import re as _re
    
    if not content:
        return (content, 0)
    
    blocks_compressed = 0
    
    pattern = r'\[START_PREV_ACTION\](.*?)\[END_PREV_ACTION\]'
    
    def _replace_block(match):
        nonlocal blocks_compressed
        blocks_compressed += 1
        
        block_inner = match.group(1)
        
        # Extract tool name
        tool_name_match = _re.search(r'\[ACTION_TYPE\]\s*\n\s*([^\n]+)', block_inner)
        tool_name = tool_name_match.group(1).strip() if tool_name_match else "unknown"
        
        # Extract args section
        args_match = _re.search(r'\[ACTION_ARG\]\s*\n(.*?)(?=\n\[RESULT\]|\n\[END)', block_inner, _re.DOTALL)
        args_text = args_match.group(1).strip() if args_match else "(none)"
        if len(args_text) > 100:
            args_text = args_text[:100] + "..."
        
        # Build compressed block
        if max_length <= 0:
            result_text = "(compressed)"
        else:
            result_text = f"(compressed from original, {max_length} chars kept)"
        
        compressed = (
            f"[START_PREV_ACTION]\n"
            f"[ACTION_TYPE]\n"
            f"{tool_name}\n"
            f"[ACTION_ARG]\n"
            f"{args_text}\n"
            f"[RESULT]\n"
            f"{result_text}\n"
            f"[END_PREV_ACTION]"
        )
        return compressed
    
    compressed = _re.sub(pattern, _replace_block, content, flags=_re.DOTALL)
    return (compressed, blocks_compressed)


def _compress_details_tags(content: str, max_length: int) -> tuple[str, int]:
    """
    Compress <details type="tool_calls"> tags in content.
    Replaces the <result> section with a truncated version.
    Returns (compressed_content, tags_compressed).
    """
    import re as _re
    
    if not content:
        return (content, 0)
    
    tags_compressed = 0
    
    pattern = r'(<details[^>]*type=["\']?tool_calls["\']?[^>]*>)(.*?)(</details>)'
    
    def _replace_details(match):
        nonlocal tags_compressed
        tags_compressed += 1
        
        opening = match.group(1)
        inner = match.group(2)
        closing = match.group(3)
        
        # Find and compress <result> section
        result_pattern = r'(<result>)(.*?)(</result>)'
        def _compress_result(rm):
            result_content = rm.group(2)
            if len(result_content) > max_length:
                return f"{rm.group(1)}{result_content[:max_length]}... (truncated by [comp]){rm.group(3)}"
            return rm.group(0)
        
        inner_compressed = _re.sub(result_pattern, _compress_result, inner, flags=_re.DOTALL)
        return f"{opening}{inner_compressed}{closing}"
    
    compressed = _re.sub(pattern, _replace_details, content, flags=_re.DOTALL | _re.IGNORECASE)
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
    - is_comp_only=True: user sent ONLY "[comp]" — caller should return auto-reply.
    - is_comp_only=False: normal compression (still forward to LLM).
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
        is_comp_only = content.strip() == _COMP_TRIGGER
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and _COMP_TRIGGER in part.get("text", ""):
                has_comp = True
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
    for i in range(last_user_idx):
        msg = messages[i]
        raw_content = msg.get("content", "")
        
        if not raw_content:
            continue
        
        if isinstance(raw_content, str):
            compressed, count1 = _compress_prev_action_blocks(raw_content, max_length)
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
    
    # 3. Insert compression marker as a system message
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
