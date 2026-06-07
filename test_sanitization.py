#!/usr/bin/env python3
"""
測試 history sanitization 功能。
驗證 <details> 標籤被正確轉換為第三人稱自然語言描述。
"""
import sys
sys.path.insert(0, "/home/user/hermes_tool_filter")

from main import sanitize_message_content, sanitize_request_messages

# ── Test 1: 標準 <details> 標籤 ──
test1 = """以下是工具執行結果：

<details type="tool_calls" done="true" name="mcp_trading_get_positions">
<summary>✅ 🔧 mcp_trading_get_positions</summary>
<arguments>{"tool_name": "mcp_trading_get_positions", "symbol": "BTC"}</arguments>
<result>{"positions": [{"symbol": "BTC", "size": 1.5, "pnl": 1200}]}</result>
</details>

這是分析結果喵～"""

result1 = sanitize_message_content(test1)
print("=" * 60)
print("TEST 1: 標準 <details> 標籤")
print("=" * 60)
print(result1)
print()

# 驗證
assert "<details" not in result1, "❌ <details> 標籤未被移除！"
assert "Tool mcp_trading_get_positions was executed with parameters" in result1, "❌ 未找到客觀描述！"
assert "and returned:" in result1, "❌ 未找到返回結果！"
assert "這是分析結果喵～" in result1, "❌ 正常文字被破壞！"
print("✅ TEST 1 PASSED\n")

# ── Test 2: 多個 <details> 標籤 ──
test2 = """先查倉位：

<details type="tool_calls" done="true" name="mcp_trading_get_positions">
<summary>✅ 🔧 mcp_trading_get_positions</summary>
<arguments>{"tool_name": "mcp_trading_get_positions"}</arguments>
<result>{"positions": []}</result>
</details>

再查餘額：

<details type="tool_calls" done="true" name="mcp_trading_get_wallet_balance">
<summary>✅ 🔧 mcp_trading_get_wallet_balance</summary>
<arguments>{"tool_name": "mcp_trading_get_wallet_balance"}</arguments>
<result>{"balance": 50000, "currency": "USDT"}</result>
</details>

兩者都查完了喵"""

result2 = sanitize_message_content(test2)
print("=" * 60)
print("TEST 2: 多個 <details> 標籤")
print("=" * 60)
print(result2)
print()

assert "<details" not in result2, "❌ <details> 標籤未被全部移除！"
assert result2.count("was executed with parameters") == 2, "❌ 應該有2個工具描述！"
assert "mcp_trading_get_positions" in result2, "❌ 第一個工具名稱遺失！"
assert "mcp_trading_get_wallet_balance" in result2, "❌ 第二個工具名稱遺失！"
assert "兩者都查完了喵" in result2, "❌ 正常文字被破壞！"
print("✅ TEST 2 PASSED\n")

# ── Test 3: 沒有 <details> 的純文字（不應該被修改） ──
test3 = """這是一段純文字，沒有任何標籤喵～
只是普通的回覆內容。"""

result3 = sanitize_message_content(test3)
print("=" * 60)
print("TEST 3: 純文字（不應該被修改）")
print("=" * 60)
assert result3 == test3, "❌ 純文字被錯誤修改！"
print("✅ TEST 3 PASSED\n")

# ── Test 4: 模型模仿輸出的 <details>（沒有 type 屬性） ──
test4 = """我來執行工具：

<details>
<summary>✅ 🔧 terminal</summary>
<arguments>{"tool_name": "terminal", "input": "ls"}</arguments>
<result>file1.txt\nfile2.txt</result>
</details>

完成了喵"""

result4 = sanitize_message_content(test4)
print("=" * 60)
print("TEST 4: 模型模仿的 <details>（無 type 屬性）")
print("=" * 60)
print(result4)
print()

assert "<details" not in result4, "❌ 無 type 的 <details> 未被移除！"
assert "工具已執行" in result4, "❌ 無 type 的 <details> 未被替換！"
print("✅ TEST 4 PASSED\n")

# ── Test 5: sanitize_request_messages 完整流程 ──
test5_messages = [
    {"role": "system", "content": "你是一個助手。"},
    {"role": "user", "content": "查一下倉位"},
    {"role": "assistant", "content": """好的喵～

<details type="tool_calls" done="true" name="terminal">
<summary>✅ 💻 terminal</summary>
<arguments>{"tool_name": "terminal", "input": "echo test"}</arguments>
<result>test</result>
</details>

查完了喵～"""},
    {"role": "user", "content": "再查一次"},
]

result5 = sanitize_request_messages(test5_messages)
print("=" * 60)
print("TEST 5: 完整 messages 清洗流程")
print("=" * 60)

# system message 不應該被修改
assert result5[0]["content"] == "你是一個助手。", "❌ system message 被修改！"
# user message 不應該被修改
assert result5[1]["content"] == "查一下倉位", "❌ user message 被修改！"
# assistant message 應該被清洗
assert "<details" not in result5[2]["content"], "❌ assistant 的 <details> 未被移除！"
assert "Tool terminal was executed with parameters" in result5[2]["content"], "❌ 未找到工具描述！"
assert "查完了喵～" in result5[2]["content"], "❌ assistant 的正常文字被破壞！"

print("System message:", result5[0]["content"])
print("User message:", result5[1]["content"])
print("Assistant message:")
print(result5[2]["content"])
print()
print("✅ TEST 5 PASSED\n")

# ── Test 6: 空值與邊界情況 ──
assert sanitize_message_content("") == "", "❌ 空字串處理錯誤！"
assert sanitize_message_content(None) == None, "❌ None 處理錯誤！"
assert sanitize_request_messages([]) == [], "❌ 空列表處理錯誤！"
assert sanitize_request_messages(None) == None, "❌ None messages 處理錯誤！"
print("=" * 60)
print("TEST 6: 邊界情況")
print("=" * 60)
print("✅ TEST 6 PASSED\n")

print("=" * 60)
print("所有測試通過！🎉")
print("=" * 60)
