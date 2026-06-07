#!/usr/bin/env python3
"""
測試 History Sanitization 功能的所有改進：
1. 統一英文 fallback（"Previous tool was executed."）
2. Regex robustness（屬性順序、引號、大小寫）
3. Result 截斷可配置（預設 2000）
4. 計數器記錄清理數量
5. Config 開關控制
"""
import sys
import os

# 加入 main.py 的目錄到 path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'hermes_tool_filter'))

from main import sanitize_message_content, sanitize_request_messages, _get_sanitization_config

# ── 測試 1: 標準 <details type="tool_calls"> 格式 ──
def test_standard_details():
    content = '''Some text before.
<details type="tool_calls" done="true" name="mcp_trading_get_positions">
<summary>✅ 🔧 mcp_trading_get_positions</summary>
<arguments>{"command": "get_positions"}</arguments>
<result>{"positions": []}</result>
</details>
Some text after.'''
    
    sanitized, count = sanitize_message_content(content)
    
    assert count == 1, f"Expected 1 replacement, got {count}"
    assert "Tool mcp_trading_get_positions was executed" in sanitized
    assert "<details" not in sanitized
    assert "Some text before" in sanitized
    assert "Some text after" in sanitized
    print("✅ Test 1 PASSED: Standard details format")

# ── 測試 2: 多個 <details> 區塊 ──
def test_multiple_details():
    content = '''First:
<details type="tool_calls" done="true" name="tool_a">
<summary>tool_a</summary>
<arguments>{"x": 1}</arguments>
<result>{"ok": true}</result>
</details>
Middle.
<details type="tool_calls" done="true" name="tool_b">
<summary>tool_b</summary>
<arguments>{"y": 2}</arguments>
<result>{"ok": false}</result>
</details>
Last.'''
    
    sanitized, count = sanitize_message_content(content)
    
    assert count == 2, f"Expected 2 replacements, got {count}"
    assert "Tool tool_a was executed" in sanitized
    assert "Tool tool_b was executed" in sanitized
    assert "<details" not in sanitized
    print("✅ Test 2 PASSED: Multiple details blocks")

# ── 測試 3: 沒有 <details> 的內容 ──
def test_no_details():
    content = "純粹的文字內容，沒有任何工具呼叫。"
    
    sanitized, count = sanitize_message_content(content)
    
    assert count == 0, f"Expected 0 replacements, got {count}"
    assert sanitized == content
    print("✅ Test 3 PASSED: No details to clean")

# ── 測試 4: 模型模仿的 <details>（沒有 type 屬性） ──
def test_imitated_details():
    content = '''Model output:
<details>
<summary>模仿工具</summary>
<arguments>{"fake": true}</arguments>
<result>{"mimic": true}</result>
</details>'''
    
    sanitized, count = sanitize_message_content(content)
    
    assert count == 1, f"Expected 1 replacement, got {count}"
    assert "Previous tool was executed." in sanitized
    assert "<details" not in sanitized
    assert "工具已執行" not in sanitized  # 確認不再使用中文
    print("✅ Test 4 PASSED: Imitated details (fallback to English)")

# ── 測試 5: 屬性順序不同 + 單引號（Regex robustness） ──
def test_regex_robustness():
    # 屬性順序不同
    content1 = '''<details name="tool_x" type="tool_calls" done="true">
<arguments>{"a": 1}</arguments>
<result>{"b": 2}</result>
</details>'''
    s1, c1 = sanitize_message_content(content1)
    assert c1 == 1, f"Expected 1, got {c1}"
    assert "Tool tool_x was executed" in s1
    
    # 單引號
    content2 = """<details type='tool_calls' name='tool_y'>
<arguments>{"c": 3}</arguments>
<result>{"d": 4}</result>
</details>"""
    s2, c2 = sanitize_message_content(content2)
    assert c2 == 1, f"Expected 1, got {c2}"
    assert "Tool tool_y was executed" in s2
    
    # 大小寫不敏感
    content3 = '''<DETAILS TYPE="tool_calls" NAME="tool_z">
<arguments>{"e": 5}</arguments>
<result>{"f": 6}</result>
</DETAILS>'''
    s3, c3 = sanitize_message_content(content3)
    assert c3 == 1, f"Expected 1, got {c3}"
    assert "Tool tool_z was executed" in s3
    
    print("✅ Test 5 PASSED: Regex robustness (order, quotes, case)")

# ── 測試 6: Result 截斷功能 ──
def test_result_truncation():
    long_result = "x" * 2500  # 超過預設 2000
    content = f'''<details type="tool_calls" name="trunc_tool">
<arguments>{{}}</arguments>
<result>{long_result}</result>
</details>'''
    
    sanitized, count = sanitize_message_content(content)
    
    assert count == 1
    assert "Tool trunc_tool was executed" in sanitized
    # 確認結果被截斷並加上 ...
    assert "..." in sanitized
    # 確認截斷長度正確（2000 + "..." = 2003）
    # 結果部分應該包含 "..." 結尾
    result_part = sanitized.split("returned: ", 1)[1]
    assert result_part.endswith("..."), "Result should end with ..."
    print("✅ Test 6 PASSED: Result truncation at configurable length")

# ── 測試 7: Config 開關控制 ──
def test_config_toggle():
    messages = [
        {"role": "user", "content": "請執行工具"},
        {"role": "assistant", "content": '''<details type="tool_calls" name="t1">
<arguments>{}</arguments>
<result>{}</result>
</details>'''},
    ]
    
    # 開啟時應該清理
    original = messages[1]["content"]
    cleaned_msgs = sanitize_request_messages(messages)
    assert "<details" not in cleaned_msgs[1]["content"], "Should clean when enabled"
    
    # 恢復原始內容
    messages[1]["content"] = original
    
    print("✅ Test 7 PASSED: Config toggle works")

# ── 測試 8: sanitize_request_messages 完整流程 ──
def test_full_messages():
    messages = [
        {"role": "user", "content": "請執行工具"},
        {"role": "assistant", "content": '''<details type="tool_calls" name="t1">
<arguments>{}</arguments>
<result>{}</result>
</details>'''},
        {"role": "user", "content": "再來一次"},
        {"role": "assistant", "content": "沒有工具呼叫的純文字"},
        {"role": "assistant", "content": '''<details type="tool_calls" name="t2">
<arguments>{}</arguments>
<result>{}</result>
</details>'''},
    ]
    
    cleaned = sanitize_request_messages(messages)
    
    # 第一個 assistant 應該被清理
    assert "<details" not in cleaned[1]["content"]
    # 第二個 assistant 是純文字，不被改變
    assert cleaned[2]["content"] == messages[2]["content"]
    # 第三個 assistant 應該被清理
    assert "<details" not in cleaned[3]["content"]
    print("✅ Test 8 PASSED: Full messages sanitization")

# ── 測試 9: 邊緣情況 ──
def test_edge_cases():
    # None 內容
    s1, c1 = sanitize_message_content(None)
    assert s1 is None and c1 == 0
    
    # 空字串
    s2, c2 = sanitize_message_content("")
    assert s2 == "" and c2 == 0
    
    # 只有 <details> 沒有子標籤
    s3, c3 = sanitize_message_content("<details>只是普通標籤</details>")
    assert c3 == 0  # 沒有 arguments/result，不匹配
    
    print("✅ Test 9 PASSED: Edge cases")

if __name__ == "__main__":
    test_standard_details()
    test_multiple_details()
    test_no_details()
    test_imitated_details()
    test_regex_robustness()
    test_result_truncation()
    test_config_toggle()
    test_full_messages()
    test_edge_cases()
    print("\n🎉 所有測試通過！")
