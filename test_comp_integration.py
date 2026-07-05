#!/usr/bin/env python3
"""
整合測試: 透過 HTTP 實際測試 [comp] 模式的運作。
"""
import sys
sys.path.insert(0, '.')

import json
import asyncio
from httpx import AsyncClient
from main import APP

BASE_URL = "http://127.0.0.1:9099"

def create_test_messages_comp_only():
    """場景1: 用戶只發送 [comp]"""
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Run a search"},
        {"role": "assistant", "content": """Let me search for that.

[START_PREV_ACTION]
[ACTION_TYPE]
search_files
[ACTION_ARG]
pattern: *.py
target: files
path: /home/thomas2018
limit: 50
[RESULT]
{
  "total_count": 50,
  "files": [
    "/home/thomas2018/main.py",
    "/home/thomas2018/config.py",
    "/home/thomas2018/test.py",
    "/home/thomas2018/utils.py",
    "/home/thomas2018/app.py"
  ]
}
[END_PREV_ACTION]

Here are the results from the search."""},
        {"role": "user", "content": "[comp]"}
    ]

def create_test_messages_comp_with_content():
    """場景2: 用戶發送 [comp] 加上其他內容"""
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Run a search"},
        {"role": "assistant", "content": """Let me search for that.

[START_PREV_ACTION]
[ACTION_TYPE]
terminal
[ACTION_ARG]
command: ls -la /home/thomas2018
[RESULT]
total 1234
drwxr-xr-x 20 thomas2018 thomas2018  4096 Jun 19 05:00 .
drwxr-xr-x  3 root       root        4096 Jan  1 00:00 ..
-rw-r--r--  1 thomas2018 thomas2018  220 Jun 19 04:55 .bash_logout
-rw-r--r--  1 thomas2018 thomas2018 3771 Jun 19 04:55 .bashrc
-rw-r--r--  1 thomas2018 thomas2018  807 Jun 19 04:55 .profile
[END_PREV_ACTION]

Here are the files in your directory."""},
        {"role": "user", "content": "Now analyze the results [comp]"}
    ]

def create_test_messages_no_comp():
    """場景3: 沒有 [comp] 標記"""
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": """Hi there!

[START_PREV_ACTION]
[ACTION_TYPE]
date
[ACTION_ARG]
[RESULT]
Fri 19 Jun 05:17:37 CST 2026
[END_PREV_ACTION]

The current time is shown above."""},
        {"role": "user", "content": "What is the time?"}
    ]

async def test_comp_only():
    """測試場景1: 純 [comp] 請求"""
    print("=" * 60)
    print("場景1: 純 [comp] 請求 (is_comp_only=True)")
    print("=" * 60)
    
    messages = create_test_messages_comp_only()
    payload = {
        "model": "test-model",
        "messages": messages,
        "stream": True
    }
    
    # 直接測試 compress_tool_results 函數
    from comp_mode import compress_tool_results
    config = {"comp_mode": "enabled", "comp_result_max_length": 100}
    
    result, is_comp_only = compress_tool_results(messages.copy(), config)
    
    print(f"\n✅ is_comp_only: {is_comp_only}")
    assert is_comp_only == True, "應該為 True"
    
    # 檢查最後一個 user message 是否被清空
    last_user = result[-1]
    print(f"✅ 最後一個 user message: '{last_user['content']}'")
    assert last_user["content"] == "", "[comp] 應該被移除"
    
    # 檢查是否插入 marker
    marker_found = any(
        msg["role"] == "system" and "CONVERSATION COMPRESSED" in msg.get("content", "")
        for msg in result
    )
    print(f"✅ Marker 已插入: {marker_found}")
    assert marker_found, "應該有壓縮標記"
    
    # 檢查 assistant message 被壓縮
    assistant_msg = result[2]
    print(f"✅ Assistant message 包含 '(compressed)': {'(compressed' in assistant_msg['content']}")
    assert "(compressed" in assistant_msg["content"]
    
    print("\n🎉 場景1 通過!\n")

async def test_comp_with_content():
    """測試場景2: [comp] 加上其他內容"""
    print("=" * 60)
    print("場景2: [comp] + 其他內容 (is_comp_only=False)")
    print("=" * 60)
    
    messages = create_test_messages_comp_with_content()
    
    from comp_mode import compress_tool_results
    config = {"comp_mode": "enabled", "comp_result_max_length": 100}
    
    result, is_comp_only = compress_tool_results(messages.copy(), config)
    
    print(f"\n✅ is_comp_only: {is_comp_only}")
    assert is_comp_only == False, "應該為 False"
    
    # 檢查 [comp] 被移除但保留其他內容
    last_user = result[-1]
    print(f"✅ 最後一個 user message: '{last_user['content']}'")
    assert "[comp]" not in last_user["content"]
    assert "analyze the results" in last_user["content"]
    
    # 檢查壓縮標記
    marker_found = any(
        msg["role"] == "system" and "CONVERSATION COMPRESSED" in msg.get("content", "")
        for msg in result
    )
    print(f"✅ Marker 已插入: {marker_found}")
    
    print("\n🎉 場景2 通過!\n")

async def test_no_comp():
    """測試場景3: 沒有 [comp]"""
    print("=" * 60)
    print("場景3: 沒有 [comp] 標記")
    print("=" * 60)
    
    messages = create_test_messages_no_comp()
    
    from comp_mode import compress_tool_results
    config = {"comp_mode": "enabled", "comp_result_max_length": 100}
    
    original_len = len(messages[2]["content"])
    result, is_comp_only = compress_tool_results(messages.copy(), config)
    
    print(f"\n✅ is_comp_only: {is_comp_only}")
    assert is_comp_only == False
    
    # 應該沒有變化
    new_len = len(result[2]["content"])
    print(f"✅ 原始長度: {original_len}, 壓縮後: {new_len}")
    assert original_len == new_len, "應該沒有被壓縮"
    
    print("\n🎉 場景3 通過!\n")

async def main():
    print("\n" + "=" * 60)
    print("開始 [comp] 模式整合測試")
    print("=" * 60 + "\n")
    
    await test_comp_only()
    await test_comp_with_content()
    await test_no_comp()
    
    print("=" * 60)
    print("所有整合測試通過! 🎉")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
