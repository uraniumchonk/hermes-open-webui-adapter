#!/usr/bin/env python3
"""Test session ID collision fix with marker-based recovery."""
import sys
sys.path.insert(0, '/home/thomas2018/hermes_tool_filter')

from main import get_or_create_session_id, _session_cache, _pending_session_marker

# Clear cache
_session_cache.clear()

def reset():
    _session_cache.clear()

def test_no_collision():
    """Two conversations with identical first message should get different session IDs."""
    reset()
    msg1 = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "你好"},
    ]
    msg2 = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "你好"},
    ]

    sid1 = get_or_create_session_id(msg1)
    sid2 = get_or_create_session_id(msg2)

    print(f"Session 1: {sid1}")
    print(f"Session 2: {sid2}")
    print(f"Msg1 first user: {msg1[1]['content'][:60]}...")
    print(f"Msg2 first user: {msg2[1]['content'][:60]}...")

    assert sid1 != sid2, f"❌ FAIL: Same session ID: {sid1}"
    print("✅ PASS: Different sessions for identical messages\n")
    return sid1, msg1


def test_same_conversation_via_marker(sid1, msg1):
    """Simulate turn 2: Open WebUI sends history + assistant response with embedded marker."""
    reset()  # Simulate a restart — cache is cold, we MUST rely on the marker

    # Re-read the marker that would have been embedded in the assistant response
    # (In reality, transform_stream embeds it; here we simulate it.)
    from main import _pending_session_marker as pending
    # After test_no_collision, _pending_session_marker holds (sid2, ts2) for msg2.
    # For msg1, we need to reconstruct. Let's just build the marker manually.
    # The marker format is: <!--session:api-HEX,ts:TIMESTAMP-->
    
    # Extract the timestamp from msg1's stamped content
    import re
    ts_match = re.search(r"<!--ts:([^>]+)-->", msg1[1]["content"])
    ts1 = ts_match.group(1) if ts_match else "unknown"
    marker = f"<!--session:{sid1},ts:{ts1}-->"

    # Simulate turn 2: history includes the original first message + assistant reply + new message
    turn2_messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "你好"},  # original, no timestamp
        {"role": "assistant", "content": f"你好！有什麼可以幫你？{marker}"},  # contains marker
        {"role": "user", "content": "你好嗎？"},
    ]

    sid_turn2 = get_or_create_session_id(turn2_messages)
    print(f"Turn 2 Session: {sid_turn2}")
    print(f"Original Session: {sid1}")
    print(f"Turn2 msg1 content: {turn2_messages[1]['content'][:60]}...")

    assert sid1 == sid_turn2, f"❌ FAIL: Different session IDs: {sid1} vs {sid_turn2}"
    print("✅ PASS: Same conversation reuses session ID via marker\n")


def test_third_conversation():
    """A third conversation with the same content gets yet another unique ID."""
    reset()
    msg1 = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "你好"},
    ]
    msg2 = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "你好"},
    ]

    sid1 = get_or_create_session_id(msg1)
    sid2 = get_or_create_session_id(msg2)

    assert sid1 != sid2, f"❌ FAIL: sid1 == sid2 = {sid1}"
    print(f"Session 1: {sid1}")
    print(f"Session 3: {sid2}")
    print("✅ PASS: Third conversation is also unique\n")


if __name__ == "__main__":
    print("=== Test 1: No collision for identical first messages ===\n")
    sid1, msg1 = test_no_collision()

    print("=== Test 2: Same conversation reuses session via marker ===\n")
    test_same_conversation_via_marker(sid1, msg1)

    print("=== Test 3: Third conversation is unique ===\n")
    test_third_conversation()

    print("=== All tests passed! ===")
