#!/usr/bin/env python3
"""Test session ID collision fix — v3 with marker-based recovery."""
import hashlib
from datetime import datetime

_session_cache = {}

def derive_session_id(messages):
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
                first_user = content
            elif isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append(p.get("text", ""))
                first_user = "".join(parts)
            break

    seed = f"{system_prompt}\n{first_user}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"api-{digest}"

def _strip_timestamp_and_derive(messages):
    import re
    ts_re = re.compile(r"^<!--ts:[^>]*-->")
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
                first_user = ts_re.sub("", content)
            elif isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append(ts_re.sub("", p.get("text", "")))
                    else:
                        parts.append(p.get("text", ""))
                first_user = "".join(parts)
            break

    seed = f"{system_prompt}\n{first_user}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"api-{digest}"

def get_or_create_session_id(messages):
    import re
    from datetime import datetime
    global _pending_session_marker

    # ── Step 1: Look for an embedded session marker in assistant history ──
    marker_pattern = re.compile(
        r"<!--session:(api-[a-f0-9]+),ts:([^>]+)-->", re.IGNORECASE
    )
    found_ts = None
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str):
                m = marker_pattern.search(content)
                if m:
                    found_ts = m.group(1), m.group(2)
                    break
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        m = marker_pattern.search(part["text"])
                        if m:
                            found_ts = m.group(1), m.group(2)
                            break
        if found_ts:
            break

    if found_ts:
        # Recovered session marker from history — use the original fingerprint
        # to look up the cached session ID.
        original_fp, recovered_ts = found_ts
        if original_fp in _session_cache:
            return _session_cache[original_fp]
        # Fallback: reconstruct stamped fingerprint and try that
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                ts_marker = f"<!--ts:{recovered_ts}-->"
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
        # Last resort: return the original fingerprint directly
        return original_fp

    # ── Step 2: Fingerprint-based cache (only hit if marker confirms same session) ──
    derived = derive_session_id(messages)
    if not derived:
        return ""

    if derived in _session_cache:
        pass

    # ── Step 3: New conversation — inject timestamp ──
    timestamp = datetime.now().isoformat()
    ts_marker = f"<!--ts:{timestamp}-->"
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
    _session_cache[original] = stamped_derived

    # Remember the marker so transform_stream can embed it in the first
    # assistant response.  Store the original fingerprint + timestamp.
    _pending_session_marker = (original, timestamp)

    return stamped_derived

_pending_session_marker = None

def update_session_id(messages, new_sid):
    original = _strip_timestamp_and_derive(messages)
    if original and new_sid:
        _session_cache[original] = new_sid

# ── Tests ──

def test_session_flow():
    global _pending_session_marker
    _session_cache.clear()
    _pending_session_marker = None

    system = {"role": "system", "content": "You are a helpful assistant."}
    user_msg = "你好"

    # ── Request 1: New conversation A ──
    msg1 = [system, {"role": "user", "content": user_msg}]
    sid1 = get_or_create_session_id(msg1)
    print(f"Step 1 - New conversation A: {sid1}")
    assert "<!--ts:" in msg1[1]["content"], "Timestamp should be injected"

    # Simulate Gateway returning a session ID (same as stamped for now)
    gateway_sid_1 = sid1

    # Simulate marker embedded in assistant response
    assistant_with_marker = {
        "role": "assistant",
        "content": f"你好！<!--session:{_pending_session_marker[0]},ts:{_pending_session_marker[1]}-->"
    }

    # Update cache with Gateway session ID
    update_session_id(msg1, gateway_sid_1)

    # ── Request 2: Same conversation, second turn (with marker in history) ──
    msg2 = [
        system,
        {"role": "user", "content": user_msg},
        assistant_with_marker,
        {"role": "user", "content": "請繼續"},
    ]
    sid2 = get_or_create_session_id(msg2)
    print(f"Step 2 - Same conversation (with marker): {sid2}")
    assert sid2 == gateway_sid_1, f"Should be same session! {sid2} != {gateway_sid_1}"

    # Simulate compression — Gateway returns a new session ID
    gateway_sid_2 = "hermes-compressed-abc123"
    update_session_id(msg2, gateway_sid_2)

    # ── Request 3: Third turn (with marker in history) ──
    msg3 = [
        system,
        {"role": "user", "content": user_msg},
        assistant_with_marker,
        {"role": "user", "content": "請繼續"},
        {"role": "assistant", "content": "好的<!--session:api-xxxx,ts:2026-06-15T05:57:15.123456-->"},
        {"role": "user", "content": "最後一個問題"},
    ]
    sid3 = get_or_create_session_id(msg3)
    print(f"Step 3 - Third turn (after compression): {sid3}")
    assert sid3 == gateway_sid_2, f"Should be compressed session! {sid3} != {gateway_sid_2}"

    # ── Request 4: New conversation B with same first message ──
    _session_cache.clear()  # Simulate fresh state for comparison
    msg4 = [system, {"role": "user", "content": user_msg}]
    sid4 = get_or_create_session_id(msg4)
    print(f"Step 4 - New conversation B (same content): {sid4}")
    assert "<!--ts:" in msg4[1]["content"], "Should inject timestamp for new conversation"
    assert sid4 != sid1, "Should be a different session!"

    print("\n✅ All tests passed!")

if __name__ == "__main__":
    test_session_flow()
