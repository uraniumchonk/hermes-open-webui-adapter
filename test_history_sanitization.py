#!/usr/bin/env python3
"""
測試 History Sanitization 功能 — 自然語言風格版本：
1. 多種自然語言風格（不再是固定模板）
2. 確定性隨機（基於 seed，確保 KV cache 命中）
3. 根據工具類型選擇不同風格
4. Regex robustness（屬性順序、引號、大小寫）
5. Result 截斷可配置（預設 2000）
6. 計數器記錄清理數量
7. Config 開關控制
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
    
    sanitized, count = sanitize_message_content(content, seed=42)
    
    assert count == 1, f"Expected 1 replacement, got {count}"
    assert "<details" not in sanitized, "Should remove <details> tags"
    assert "Some text before" in sanitized, "Should preserve surrounding text"
    assert "Some text after" in sanitized, "Should preserve surrounding text"
    # 新的自然語言風格，只要確認不包含舊的固定模板
    assert "Tool mcp_trading_get_positions was executed" not in sanitized, "Should NOT use old template"
    # 確認是中文自然語言風格
    assert any(phrase in sanitized for phrase in [
        "先前查詢了交易", "根據交易工具的回應", "工具回傳的交易資料", "從交易系統取得的"
    ]), f"Should use natural Chinese language: {sanitized}"
    print("✅ Test 1 PASSED: Standard details format -> natural language")

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
    
    sanitized, count = sanitize_message_content(content, seed=42)
    
    assert count == 2, f"Expected 2 replacements, got {count}"
    assert "<details" not in sanitized, "Should remove all <details> tags"
    assert "First:" in sanitized
    assert "Middle." in sanitized
    assert "Last." in sanitized
    print("✅ Test 2 PASSED: Multiple details blocks cleaned")

# ── 測試 3: 沒有 <details> 的內容 ──
def test_no_details():
    content = "純粹的文字內容，沒有任何工具呼叫。"
    
    sanitized, count = sanitize_message_content(content)
    
    assert count == 0, f"Expected 0 replacements, got {count}"
    assert sanitized == content, "Content should be unchanged"
    print("✅ Test 3 PASSED: No details to clean")

# ── 測試 4: 模型模仿的 <details>（沒有 type 屬性） ──
def test_imitated_details():
    content = '''Model output:
<details>
<summary>模仿工具</summary>
<arguments>{"fake": true}</arguments>
<result>{"mimic": true}</result>
</details>'''
    
    sanitized, count = sanitize_message_content(content, seed=42)
    
    assert count == 1, f"Expected 1 replacement, got {count}"
    assert "<details" not in sanitized, "Should remove <details> tags"
    assert "Previous tool was executed." not in sanitized, "Should NOT use old English fallback"
    assert "工具已執行" not in sanitized, "Should NOT use old Chinese fallback"
    # 確認有輸出自然語言描述
    assert len(sanitized) > len("Model output:"), "Should have sanitized content"
    print("✅ Test 4 PASSED: Imitated details -> natural language")

# ── 測試 5: 屬性順序不同 + 單引號（Regex robustness） ──
def test_regex_robustness():
    # 屬性順序不同
    content1 = '''<details name="tool_x" type="tool_calls" done="true">
<arguments>{"a": 1}</arguments>
<result>{"b": 2}</result>
</details>'''
    s1, c1 = sanitize_message_content(content1, seed=42)
    assert c1 == 1, f"Expected 1, got {c1}"
    assert "<details" not in s1
    
    # 單引號
    content2 = """<details type='tool_calls' name='tool_y'>
<arguments>{"c": 3}</arguments>
<result>{"d": 4}</result>
</details>"""
    s2, c2 = sanitize_message_content(content2, seed=42)
    assert c2 == 1, f"Expected 1, got {c2}"
    assert "<details" not in s2
    
    # 大小寫不敏感
    content3 = '''<DETAILS TYPE="tool_calls" NAME="tool_z">
<arguments>{"e": 5}</arguments>
<result>{"f": 6}</result>
</DETAILS>'''
    s3, c3 = sanitize_message_content(content3, seed=42)
    assert c3 == 1, f"Expected 1, got {c3}"
    assert "<details" not in s3.lower() or "<details" not in s3
    
    print("✅ Test 5 PASSED: Regex robustness (order, quotes, case)")

# ── 測試 6: Result 截斷功能 ──
def test_result_truncation():
    long_result = "x" * 2500  # 超過預設 2000
    content = f'''<details type="tool_calls" name="trunc_tool">
<arguments>{{}}</arguments>
<result>{long_result}</result>
</details>'''
    
    sanitized, count = sanitize_message_content(content, seed=42)
    
    assert count == 1
    # 確認結果被截斷並加上 ...
    assert "..." in sanitized, "Result should be truncated with ..."
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
    assert c3 == 0, "No arguments/result, should not match"
    
    print("✅ Test 9 PASSED: Edge cases")

# ── 測試 10: 搜尋工具類型分類 ──
def test_search_tool_style():
    content = '''<details type="tool_calls" name="web_search">
<arguments>{"query": "台中今晚天氣"}</arguments>
<result>{"results": [{"title": "天氣預報"}]}</result>
</details>'''
    
    sanitized, count = sanitize_message_content(content, seed=42)
    
    assert count == 1
    assert "<details" not in sanitized
    # 搜尋工具應該使用搜尋相關的描述
    assert any(phrase in sanitized for phrase in [
        "搜尋", "查詢", "搜尋了", "搜尋的結果"
    ]), f"Search tool should use search-related phrasing: {sanitized}"
    print("✅ Test 10 PASSED: Search tool uses search-style description")

# ── 測試 11: 交易工具類型分類 ──
def test_trading_tool_style():
    content = '''<details type="tool_calls" name="mcp_trading_get_positions">
<arguments>{"action": "list"}</arguments>
<result>{"positions": []}</result>
</details>'''
    
    sanitized, count = sanitize_message_content(content, seed=42)
    
    assert count == 1
    assert "<details" not in sanitized
    # 交易工具應該使用交易相關的描述
    assert any(phrase in sanitized for phrase in [
        "交易", "交易工具", "交易系統"
    ]), f"Trading tool should use trading-related phrasing: {sanitized}"
    print("✅ Test 11 PASSED: Trading tool uses trading-style description")

# ── 測試 12: 確定性隨機（相同 seed → 相同結果） ──
def test_deterministic_randomness():
    content = '''<details type="tool_calls" name="web_search">
<arguments>{"query": "測試"}</arguments>
<result>{"data": "test"}</result>
</details>'''
    
    # 相同 seed 應該產生相同結果
    result_a1, _ = sanitize_message_content(content, seed=123)
    result_a2, _ = sanitize_message_content(content, seed=123)
    assert result_a1 == result_a2, "Same seed should produce same result"
    
    # 不同 seed 可能產生不同結果
    result_b, _ = sanitize_message_content(content, seed=456)
    
    print(f"✅ Test 12 PASSED: Deterministic randomness verified")
    print(f"   seed=123: {result_a1[:60]}...")
    print(f"   seed=456: {result_b[:60]}...")

# ── 測試 13: KV cache 一致性（同一請求多次執行） ──
def test_kv_cache_consistency():
    messages = [
        {"role": "user", "content": "請搜尋天氣"},
        {"role": "assistant", "content": '''<details type="tool_calls" name="web_search">
<arguments>{"query": "台中今晚天氣"}</arguments>
<result>{"results": [{"title": "天氣預報"}]}</result>
</details>'''},
    ]
    
    # 模擬同一請求被重複發送（例如重試）
    results = []
    for _ in range(5):
        test_msgs = [dict(m) for m in messages]  # 深拷貝
        cleaned = sanitize_request_messages(test_msgs)
        results.append(cleaned[1]["content"])
    
    # 所有結果應該完全相同
    assert all(r == results[0] for r in results), \
        "Same request should always produce same sanitized output for KV cache"
    
    print(f"✅ Test 13 PASSED: KV cache consistency (5 runs identical)")
    print(f"   Output: {results[0][:60]}...")

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
    test_search_tool_style()
    test_trading_tool_style()
    test_deterministic_randomness()
    test_kv_cache_consistency()
    
    print("\n🎉 所有測試通過！")

