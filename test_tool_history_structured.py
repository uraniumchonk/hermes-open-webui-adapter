"""
test_tool_history_structured — 結構化 tool-role 轉換引擎的單元測試。

測試範圍（對應 PLAN_tool_role_restructure.md Step 1 的驗收案例）：
    a. 無 <details> → 原樣透傳單條 assistant
    b. 單一 <details> 夾在文字中間 → 3 條 messages，tool_call_id 正確配對
    c. 多個 <details> → assistant/tool 交替
    d. <details> 只在開頭 / 只在結尾 → 不出現沒有 tool_calls 的空 assistant
    e. arguments 解析失敗 / 缺少 <arguments> → arguments 為 "{}"
    f. 長 result 依 max_result_length 截斷
    g. 已有 tool_calls → 原樣不變
    h. 混合 user/assistant/user → 只有含 details 的 assistant 展開，順序保留
    i. tool_name 帶引號 → 去除引號
    j. 多個工具 call_id 各自唯一
    另含：type=tool_calls 不帶引號、無 type 屬性（模型模仿格式）、
          parse 快速路徑 / 純空白、segments_to_messages 直接呼叫、
          config 截斷、不變異原列表、換行保留。
"""

import json
import unittest

from tool_history_structured import (
    parse_assistant_content,
    segments_to_messages,
    convert_assistant_message,
    sanitize_messages_structured,
)


def make_details(
    name="web_search",
    args='{"query":"天氣"}',
    result="晴天，25度",
    type_attr='type="tool_calls"',
    with_summary=True,
    name_attr=None,
):
    """產生 Open WebUI 風格的 <details type="tool_calls"> HTML 區塊。"""
    if name_attr is None:
        name_attr = f'name="{name}"'
    parts = [f"<details {type_attr} {name_attr}>"]
    if with_summary:
        parts.append(f"<summary>{name}</summary>")
    parts.append(f"<arguments>{args}</arguments>")
    parts.append(f"<result>{result}</result>")
    parts.append("</details>")
    return "\n".join(parts)


class TestParseAssistantContent(unittest.TestCase):
    """parse_assistant_content 的行為。"""

    def test_parse_no_details_fast_path(self):
        self.assertEqual(
            parse_assistant_content("你好，這是純文字。"),
            [{"type": "text", "text": "你好，這是純文字。"}],
        )

    def test_parse_empty_content(self):
        self.assertEqual(parse_assistant_content(""), [])
        self.assertEqual(parse_assistant_content(None), [])

    def test_parse_whitespace_only(self):
        self.assertEqual(parse_assistant_content("   \n  "), [])

    def test_parse_segments_structure(self):
        d = make_details()
        segs = parse_assistant_content(f"前{d}後")
        self.assertEqual([s["type"] for s in segs], ["text", "tool", "text"])
        self.assertEqual(segs[0]["text"], "前")
        self.assertEqual(segs[2]["text"], "後")
        info = segs[1]["info"]
        self.assertEqual(info["args_obj"], {"query": "天氣"})
        self.assertEqual(info["result_summary"], "晴天，25度")
        self.assertEqual(info["result_raw"], "晴天，25度")
        self.assertFalse(info["truncated"])

    def test_parse_drops_whitespace_only_text_segments(self):
        d = make_details()
        segs = parse_assistant_content(f"\n\n{d}\n\n")
        self.assertEqual([s["type"] for s in segs], ["tool"])

    def test_parse_details_without_tool_shape_kept_as_text(self):
        # 有 <details> 但沒有 arguments/result → 不是工具區塊，整段當文字
        segs = parse_assistant_content("請參考 <details> 說明文件</details>")
        self.assertEqual(segs, [{"type": "text", "text": "請參考 <details> 說明文件</details>"}])


class TestConvertAssistantMessage(unittest.TestCase):
    """convert_assistant_message 的行為。"""

    # --- b. 單一 details 夾在文字中間 ---
    def test_single_details_mid_text(self):
        d = make_details()
        content = f"讓我查一下。{d}查完了！"
        out = convert_assistant_message({"role": "assistant", "content": content})
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["role"], "assistant")
        self.assertEqual(out[0]["content"], "讓我查一下。")
        self.assertEqual(len(out[0]["tool_calls"]), 1)
        tc = out[0]["tool_calls"][0]
        self.assertEqual(tc["type"], "function")
        self.assertEqual(tc["function"]["name"], "web_search")
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"query": "天氣"})
        self.assertTrue(tc["id"].startswith("call_htf_"))
        self.assertEqual(out[1]["role"], "tool")
        self.assertEqual(out[1]["tool_call_id"], tc["id"])
        self.assertEqual(out[1]["content"], "晴天，25度")
        self.assertEqual(out[2], {"role": "assistant", "content": "查完了！"})

    def test_newline_preserved_in_text_segments(self):
        d = make_details()
        out = convert_assistant_message(
            {"role": "assistant", "content": f"前\n\n{d}\n\n後"}
        )
        self.assertEqual(out[0]["content"], "前\n\n")
        self.assertEqual(out[2]["content"], "\n\n後")

    # --- c. 多個 details → 交替 ---
    def test_multiple_details_alternating(self):
        d1 = make_details(name="web_search", args='{"query":"A"}', result="R1")
        d2 = make_details(name="read_file", args='{"path":"/tmp/x"}', result="R2")
        content = f"前文{d1}中間{d2}後文"
        out = convert_assistant_message({"role": "assistant", "content": content})
        self.assertEqual(len(out), 5)
        self.assertEqual([m["role"] for m in out],
                         ["assistant", "tool", "assistant", "tool", "assistant"])
        self.assertEqual(out[0]["content"], "前文")
        self.assertEqual(out[0]["tool_calls"][0]["function"]["name"], "web_search")
        self.assertEqual(out[1]["tool_call_id"], out[0]["tool_calls"][0]["id"])
        self.assertEqual(out[2]["content"], "中間")
        self.assertEqual(out[2]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(out[3]["tool_call_id"], out[2]["tool_calls"][0]["id"])
        self.assertEqual(out[4], {"role": "assistant", "content": "後文"})

    def test_consecutive_tools_each_get_own_pair(self):
        d = make_details()
        out = convert_assistant_message({"role": "assistant", "content": f"{d}{d}"})
        # 兩個相鄰工具：各自有自己的 assistant+tool 配對
        self.assertEqual(len(out), 4)
        self.assertEqual([m["role"] for m in out],
                         ["assistant", "tool", "assistant", "tool"])
        self.assertNotEqual(out[0]["tool_calls"][0]["id"], out[2]["tool_calls"][0]["id"])
        self.assertEqual(out[1]["tool_call_id"], out[0]["tool_calls"][0]["id"])
        self.assertEqual(out[3]["tool_call_id"], out[2]["tool_calls"][0]["id"])

    # --- d. 開頭 / 結尾 ---
    def test_details_only_at_start(self):
        d = make_details()
        out = convert_assistant_message({"role": "assistant", "content": f"{d}查完了！"})
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["content"], "")
        self.assertIn("tool_calls", out[0])
        self.assertEqual(out[2], {"role": "assistant", "content": "查完了！"})

    def test_details_only_at_end(self):
        d = make_details()
        out = convert_assistant_message({"role": "assistant", "content": f"讓我查一下。{d}"})
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["content"], "讓我查一下。")
        self.assertIn("tool_calls", out[0])
        self.assertEqual(out[1]["role"], "tool")
        # 結尾沒有文字 → 不產生多餘的空 assistant

    def test_only_details_no_surrounding_text(self):
        d = make_details()
        out = convert_assistant_message({"role": "assistant", "content": d})
        self.assertEqual(len(out), 2)
        self.assertEqual([m["role"] for m in out], ["assistant", "tool"])
        self.assertEqual(out[0]["content"], "")

    # --- e. arguments 失敗 / 缺少 ---
    def test_args_parse_failure(self):
        d = make_details(args="not-json{{{")
        out = convert_assistant_message({"role": "assistant", "content": f"前{d}後"})
        self.assertEqual(out[0]["tool_calls"][0]["function"]["arguments"], "{}")

    def test_missing_args(self):
        d = (
            '<details type="tool_calls" name="web_search">\n'
            "<result>R</result>\n"
            "</details>"
        )
        out = convert_assistant_message({"role": "assistant", "content": f"前{d}後"})
        self.assertEqual(out[0]["tool_calls"][0]["function"]["arguments"], "{}")
        self.assertEqual(out[1]["content"], "R")

    # --- f. 長 result 截斷 ---
    def test_long_result_truncated(self):
        long_result = "x" * 5000
        d = make_details(result=long_result)
        out = convert_assistant_message(
            {"role": "assistant", "content": f"前{d}後"}, max_result_length=50
        )
        self.assertEqual(len(out[1]["content"]), 53)  # 50 + "..."
        self.assertTrue(out[1]["content"].endswith("..."))
        self.assertEqual(out[1]["content"][:50], "x" * 50)

    # --- g. 已有 tool_calls → 原樣 ---
    def test_already_native_passthrough(self):
        msg = {
            "role": "assistant",
            "content": "hi",
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "x", "arguments": "{}"},
            }],
        }
        out = convert_assistant_message(msg)
        self.assertEqual(out, [msg])

    def test_tool_calls_empty_list_passthrough(self):
        # 有 tool_calls key（即使是空 list）→ 視為已是原生格式
        msg = {"role": "assistant", "content": "hi", "tool_calls": []}
        out = convert_assistant_message(msg)
        self.assertEqual(out, [msg])

    # --- a. 無 details → 原樣透傳 ---
    def test_no_details_passthrough(self):
        msg = {"role": "assistant", "content": "你好，這是純文字。"}
        out = convert_assistant_message(msg)
        self.assertEqual(out, [msg])

    def test_whitespace_only_content_passthrough(self):
        msg = {"role": "assistant", "content": "   \n "}
        out = convert_assistant_message(msg)
        self.assertEqual(out, [msg])

    def test_empty_content_passthrough(self):
        msg = {"role": "assistant", "content": ""}
        out = convert_assistant_message(msg)
        self.assertEqual(out, [msg])

    def test_non_string_content_passthrough(self):
        msg = {"role": "assistant", "content": ["text", "part"]}
        out = convert_assistant_message(msg)
        self.assertEqual(out, [msg])

    def test_details_without_tool_shape_passthrough(self):
        # 有 <details> 字樣但不像工具區塊 → 原 message 不變
        msg = {"role": "assistant", "content": "請參考 <details> 說明文件</details>"}
        out = convert_assistant_message(msg)
        self.assertEqual(out, [msg])

    # --- i. tool_name 帶引號 → 去除 ---
    def test_name_quotes_stripped(self):
        for name_attr in ('name="web_search"', "name='web_search'"):
            d = make_details(name_attr=name_attr)
            out = convert_assistant_message({"role": "assistant", "content": f"前{d}後"})
            self.assertEqual(
                out[0]["tool_calls"][0]["function"]["name"], "web_search",
                f"name_attr={name_attr} 應去除引號",
            )

    def test_type_without_quotes(self):
        d = make_details(type_attr="type=tool_calls")
        out = convert_assistant_message({"role": "assistant", "content": f"前{d}後"})
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["tool_calls"][0]["function"]["name"], "web_search")

    def test_type_attr_after_name(self):
        d = make_details(type_attr='type="tool_calls"', name_attr='name="web_search"')
        # 屬性順序對調
        d = d.replace('type="tool_calls" name="web_search"', 'name="web_search" type="tool_calls"')
        out = convert_assistant_message({"role": "assistant", "content": f"前{d}後"})
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["tool_calls"][0]["function"]["name"], "web_search")

    # --- 模型模仿格式：無 type 屬性但有 summary/arguments/result ---
    def test_details_without_type_attribute(self):
        d = make_details(type_attr="")
        out = convert_assistant_message({"role": "assistant", "content": f"前{d}後"})
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["tool_calls"][0]["function"]["name"], "web_search")
        self.assertEqual(out[1]["content"], "晴天，25度")

    # --- j. call_id 唯一 ---
    def test_call_ids_unique_across_many_tools(self):
        d = make_details()
        # range(6) → 5 個間隙 → 5 個工具區塊（後接 "t5" 尾文字）
        content = d.join([f"t{i}" for i in range(6)])
        out = convert_assistant_message({"role": "assistant", "content": content})
        ids = [m["tool_calls"][0]["id"] for m in out if m.get("tool_calls")]
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)


class TestSegmentsToMessages(unittest.TestCase):
    """segments_to_messages 的直接行為。"""

    def test_direct_segments_pairing(self):
        info = {
            "tool_name": "web_search",
            "args_obj": {"q": 1},
            "result_summary": "R",
            "result_raw": "R",
            "truncated": False,
        }
        segs = [
            {"type": "text", "text": "hi"},
            {"type": "tool", "info": info},
            {"type": "text", "text": "bye"},
        ]
        out = segments_to_messages(segs, call_id_prefix="abc")
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["content"], "hi")
        self.assertEqual(out[0]["tool_calls"][0]["function"]["name"], "web_search")
        self.assertEqual(json.loads(out[0]["tool_calls"][0]["function"]["arguments"]), {"q": 1})
        self.assertTrue(out[0]["tool_calls"][0]["id"].startswith("abc_"))
        self.assertEqual(out[1]["tool_call_id"], out[0]["tool_calls"][0]["id"])
        self.assertEqual(out[2], {"role": "assistant", "content": "bye"})

    def test_args_none_and_result_fallback_raw(self):
        info = {
            "tool_name": "x",
            "args_obj": None,
            "result_summary": "",
            "result_raw": "RAW",
            "truncated": False,
        }
        out = segments_to_messages([{"type": "tool", "info": info}])
        self.assertEqual(out[0]["tool_calls"][0]["function"]["arguments"], "{}")
        self.assertEqual(out[1]["content"], "RAW")

    def test_whitespace_text_does_not_create_junk_assistant(self):
        info = {
            "tool_name": "x", "args_obj": None,
            "result_summary": "R", "result_raw": "R", "truncated": False,
        }
        out = segments_to_messages(
            [{"type": "text", "text": "   "}, {"type": "tool", "info": info}]
        )
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["content"], "")  # 空白文字不附著

    def test_default_prefix(self):
        info = {
            "tool_name": "x", "args_obj": None,
            "result_summary": "R", "result_raw": "R", "truncated": False,
        }
        out = segments_to_messages([{"type": "tool", "info": info}])
        self.assertTrue(out[0]["tool_calls"][0]["id"].startswith("call_htf_"))


class TestSanitizeMessagesStructured(unittest.TestCase):
    """sanitize_messages_structured 的行為。"""

    # --- h. 混合 conversation，順序保留 ---
    def test_mixed_conversation_order_preserved(self):
        d = make_details()
        messages = [
            {"role": "user", "content": "天氣如何？"},
            {"role": "assistant", "content": f"讓我查。{d}查完了。"},
            {"role": "user", "content": "謝謝"},
        ]
        out = sanitize_messages_structured(messages)
        self.assertEqual(len(out), 5)
        self.assertEqual(
            [m["role"] for m in out],
            ["user", "assistant", "tool", "assistant", "user"],
        )
        self.assertIs(out[0], messages[0])
        self.assertIs(out[4], messages[2])

    def test_all_passthrough_when_no_details(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        out = sanitize_messages_structured(messages)
        self.assertEqual(out, messages)

    def test_config_max_length_applied(self):
        d = make_details(result="y" * 1000)
        messages = [{"role": "assistant", "content": f"前{d}後"}]
        out = sanitize_messages_structured(
            messages, {"sanitization_result_max_length": 30}
        )
        self.assertEqual(len(out[1]["content"]), 33)  # 30 + "..."

    def test_config_none_uses_default(self):
        d = make_details(result="z" * 100)
        messages = [{"role": "assistant", "content": f"前{d}後"}]
        out = sanitize_messages_structured(messages, None)
        self.assertEqual(out[1]["content"], "z" * 100)  # 未超過 20000 預設

    def test_original_list_not_mutated(self):
        d = make_details()
        messages = [{"role": "assistant", "content": f"前{d}後"}]
        orig = list(messages)
        out = sanitize_messages_structured(messages)
        self.assertEqual(messages, orig)
        self.assertIsNot(out, messages)
        self.assertEqual(len(messages), 1)

    def test_empty_messages(self):
        self.assertEqual(sanitize_messages_structured([]), [])

    def test_non_dict_message_passthrough(self):
        messages = ["just-a-string", {"role": "user", "content": "hi"}]
        out = sanitize_messages_structured(messages)
        self.assertEqual(out, messages)


if __name__ == "__main__":
    unittest.main()
