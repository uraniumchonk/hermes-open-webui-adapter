#!/usr/bin/env python3
"""
Test script for [comp] client-side conversation compression feature.
"""
import sys
sys.path.insert(0, '.')

from main import (
    compress_tool_results,
    _compress_prev_action_blocks,
    _COMP_TRIGGER,
    _COMP_NOTIFICATION,
)

# Mock CONFIG
TEST_CONFIG = {
    "comp_mode": "enabled",
    "comp_result_max_length": 100,
}

def test_no_comp_trigger():
    """Test: no [comp] in message — should return unchanged."""
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    result, is_comp_only = compress_tool_results(messages, TEST_CONFIG)
    assert result is messages, "Should return the same list when no [comp]"
    assert is_comp_only == False, "is_comp_only should be False"
    assert "comp" not in result[-1]["content"].lower() or result[-1]["role"] != "system"
    print("✅ test_no_comp_trigger passed")

def test_comp_compression():
    """Test: [comp] triggers compression of tool results."""
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Search for something"},
        {"role": "assistant", "content": "Let me search for that.\n\n[START_PREV_ACTION]\n[ACTION_TYPE]\nsearch_files\n[ACTION_ARG]\npattern: *.py\ntarget: content\n[RESULT]\nThis is a very long result that contains a lot of data which should be compressed down to save space in the context window. We need to make sure this gets truncated properly.\n[END_PREV_ACTION]\n\nHere are the results."},
        {"role": "user", "content": "Now do something else [comp]"},
    ]
    
    result, is_comp_only = compress_tool_results(messages, TEST_CONFIG)
    
    # [comp] should be stripped from the last user message
    last_user = result[-1]
    assert last_user["role"] == "user"
    assert _COMP_TRIGGER not in last_user["content"]
    assert "Now do something else" in last_user["content"]
    assert is_comp_only == False, "is_comp_only should be False (has other content)"
    
    # A system marker should be inserted before the last user message
    marker_found = False
    for msg in result:
        if msg["role"] == "system" and _COMP_NOTIFICATION in msg["content"]:
            marker_found = True
            break
    assert marker_found, "Compression marker should be inserted"
    
    # The assistant message should have compressed tool blocks
    assistant_msg = result[2]
    assert "(compressed" in assistant_msg["content"], "Tool result should be compressed"
    assert "[START_PREV_ACTION]" in assistant_msg["content"], "Block structure preserved"
    assert "search_files" in assistant_msg["content"], "Tool name preserved"
    
    print("✅ test_comp_compression passed")

def test_comp_disabled():
    """Test: comp_mode disabled — should not compress."""
    config_disabled = {
        "comp_mode": "disabled",
        "comp_result_max_length": 100,
    }
    messages = [
        {"role": "user", "content": "Hello [comp]"},
        {"role": "assistant", "content": "[START_PREV_ACTION]\n[ACTION_TYPE]\ntest\n[ACTION_ARG]\narg: value\n[RESULT]\nLong result\n[END_PREV_ACTION]"},
    ]
    result, is_comp_only = compress_tool_results(messages, config_disabled)
    assert result is messages, "Should return unchanged when disabled"
    assert is_comp_only == False, "is_comp_only should be False when disabled"
    print("✅ test_comp_disabled passed")

def test_block_compression_direct():
    """Test: _compress_prev_action_blocks directly."""
    content = """Some text before.
[START_PREV_ACTION]
[ACTION_TYPE]
search_files
[ACTION_ARG]
pattern: *.py
limit: 50
[RESULT]
total_count: 5
matches[0].path: main.py
matches[0].line: 42
matches[1].path: config.py
matches[1].line: 10
This result section is quite long and should be compressed.
[END_PREV_ACTION]
Some text after."""
    
    compressed, count = _compress_prev_action_blocks(content, max_length=100)
    
    assert count == 1, f"Should compress 1 block, got {count}"
    assert "search_files" in compressed, "Tool name preserved"
    assert "pattern: *.py" in compressed, "Args preserved"
    assert "(compressed" in compressed, "Result marked as compressed"
    assert "[START_PREV_ACTION]" in compressed, "Block structure preserved"
    assert "[END_PREV_ACTION]" in compressed, "Block structure preserved"
    
    print("✅ test_block_compression_direct passed")
    print(f"  Original length: {len(content)}")
    print(f"  Compressed length: {len(compressed)}")

def test_zero_max_length():
    """Test: max_length=0 completely empties result."""
    messages = [
        {"role": "user", "content": "Run tool"},
        {"role": "assistant", "content": "[START_PREV_ACTION]\n[ACTION_TYPE]\ntest\n[ACTION_ARG]\nkey: val\n[RESULT]\nVery long result data here\n[END_PREV_ACTION]"},
        {"role": "user", "content": "Continue [comp]"},
    ]
    
    config = {"comp_mode": "enabled", "comp_result_max_length": 0}
    result, is_comp_only = compress_tool_results(messages, config)
    
    assistant = result[1]
    assert "(compressed)" in assistant["content"], "Result should be (compressed) when max_length=0"
    assert is_comp_only == False, "is_comp_only should be False (has other content)"
    
    print("✅ test_zero_max_length passed")

def test_list_content():
    """Test: content as list of parts."""
    messages = [
        {"role": "user", "content": "Search"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "[START_PREV_ACTION]\n[ACTION_TYPE]\nterminal\n[ACTION_ARG]\ncommand: ls\n[RESULT]\nFile1.py\nFile2.py\n[END_PREV_ACTION]"},
        ]},
        {"role": "user", "content": "Next [comp]"},
    ]
    
    result, is_comp_only = compress_tool_results(messages, TEST_CONFIG)
    
    last_user = result[-1]
    assert _COMP_TRIGGER not in last_user["content"]
    assert is_comp_only == False, "is_comp_only should be False (has other content)"
    
    assistant = result[1]
    assert "(compressed" in assistant["content"][0]["text"]
    
    print("✅ test_list_content passed")

def test_comp_only():
    """Test: user sends ONLY [comp] — is_comp_only should be True."""
    messages = [
        {"role": "user", "content": "Run tool"},
        {"role": "assistant", "content": "[START_PREV_ACTION]\n[ACTION_TYPE]\ntest\n[ACTION_ARG]\nkey: val\n[RESULT]\nVery long result data here\n[END_PREV_ACTION]"},
        {"role": "user", "content": "[comp]"},
    ]
    
    result, is_comp_only = compress_tool_results(messages, TEST_CONFIG)
    
    # is_comp_only should be True
    assert is_comp_only == True, "is_comp_only should be True when only [comp]"
    
    # [comp] should be stripped from the last user message
    last_user = result[-1]
    assert last_user["role"] == "user"
    assert last_user["content"] == "", "[comp] should be stripped leaving empty string"
    
    # A system marker should be inserted
    marker_found = False
    for msg in result:
        if msg["role"] == "system" and _COMP_NOTIFICATION in msg["content"]:
            marker_found = True
            break
    assert marker_found, "Compression marker should be inserted"
    
    print("✅ test_comp_only passed")

def test_details_tag_compression():
    """Test: <details type="tool_calls"> tags are compressed correctly."""
    from comp_mode import _compress_details_tags
    
    content = '''Here is the result:
<details type="tool_calls">
<tool_code>search_files</tool_code>
<tool_args>pattern: *.py</tool_args>
<result>
This is a very long result that contains a lot of data which should be compressed down to save space in the context window. We need to make sure this gets truncated properly when max_length is set to a small value like 50 characters.
</result>
</details>

Done!'''
    
    compressed, count = _compress_details_tags(content, max_length=50)
    
    assert count == 1, f"Should compress 1 tag, got {count}"
    assert "truncated by [comp]" in compressed, "Should contain truncation marker"
    assert "<details" in compressed, "Should preserve details structure"
    assert len(compressed) < len(content), "Should be shorter than original"
    
    print("✅ test_details_tag_compression passed")
    print(f"  Original length: {len(content)}")
    print(f"  Compressed length: {len(compressed)}")

if __name__ == "__main__":
    print("Running [comp] compression tests...\n")
    
    test_no_comp_trigger()
    test_comp_compression()
    test_comp_disabled()
    test_block_compression_direct()
    test_zero_max_length()
    test_list_content()
    test_comp_only()
    test_details_tag_compression()
    
    print("\n🎉 All tests passed!")
