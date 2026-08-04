"""Unit tests for tool_history_structured — OpenAI native tool role conversion."""

from __future__ import annotations

import json
import sys
import unittest

sys.path.insert(0, "/home/thomas2018/hermes_tool_filter")

from tool_history_structured import (
    convert_assistant_message,
    parse_assistant_content,
    sanitize_messages_structured,
    segments_to_messages,
)


def _details(
    name: str = "web_search",
    args: str = '{"query":"天氣"}',
    result: str = "晴天，25度",
    quoted_type: bool = True,
    quoted_name: bool = True,
) -> str:
    t = 'type="tool_calls"' if quoted_type else "type=tool_calls"
    n = f'name="{name}"' if quoted_name else f"name={name}"
    return (
        f"<details {t} {n}>\n"
        f"<summary>{name}</summary>\n"
        f"<arguments>{args}</arguments>\n"
        f"<result>{result}</result>\n"
        f"</details>"
    )


class TestParseAssistantContent(unittest.TestCase):
    def test_no_details_passthrough(self):
        segs = parse_assistant_content("純文字回覆")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["type"], "text")
        self.assertEqual(segs[0]["text"], "純文字回覆")

    def test_empty(self):
        self.assertEqual(parse_assistant_content(""), [])

    def test_single_mid_text(self):
        content = f"讓我查一下。\n\n{_details()}\n\n查完了！"
        segs = parse_assistant_content(content)
        types = [s["type"] for s in segs]
        self.assertEqual(types, ["text", "tool", "text"])
        self.assertIn("讓我查一下", segs[0]["text"])
        self.assertEqual(segs[1]["info"]["tool_name"], "web_search")
        self.assertIn("查完了", segs[2]["text"])

    def test_multiple_details(self):
        content = (
            f"A\n{_details(name='terminal', args='{{\"command\":\"uptime\"}}', result='ok')}\n"
            f"B\n{_details(name='web_search', args='{{\"query\":\"x\"}}', result='y')}\nC"
        )
        segs = parse_assistant_content(content)
        tool_names = [s["info"]["tool_name"] for s in segs if s["type"] == "tool"]
        self.assertEqual(tool_names, ["terminal", "web_search"])
        self.assertEqual(sum(1 for s in segs if s["type"] == "text"), 3)

    def test_quoted_name_stripped(self):
        tag = _details(quoted_name=True)
        segs = parse_assistant_content(f"x\n{tag}\ny")
        tool = next(s for s in segs if s["type"] == "tool")
        self.assertEqual(tool["info"]["tool_name"], "web_search")
        self.assertNotIn('"', tool["info"]["tool_name"])

    def test_unquoted_type_attr(self):
        content = f"hi\n{_details(quoted_type=False)}\nbye"
        segs = parse_assistant_content(content)
        self.assertTrue(any(s["type"] == "tool" for s in segs))


class TestSegmentsToMessages(unittest.TestCase):
    def test_single_tool_three_messages(self):
        content = f"讓我查一下。\n\n{_details()}\n\n查完了！"
        segs = parse_assistant_content(content)
        msgs = segments_to_messages(segs)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0]["role"], "assistant")
        self.assertIn("tool_calls", msgs[0])
        self.assertEqual(msgs[1]["role"], "tool")
        self.assertEqual(msgs[2]["role"], "assistant")
        self.assertEqual(msgs[2]["content"].strip(), "查完了！")

        tc = msgs[0]["tool_calls"][0]
        self.assertEqual(tc["type"], "function")
        self.assertEqual(tc["function"]["name"], "web_search")
        args = json.loads(tc["function"]["arguments"])
        self.assertEqual(args.get("query"), "天氣")
        self.assertEqual(msgs[1]["tool_call_id"], tc["id"])
        self.assertTrue(tc["id"].startswith("call_htf_"))
        self.assertIn("晴天", msgs[1]["content"])

    def test_call_ids_unique(self):
        content = (
            f"{_details(name='a', args='{{}}', result='1')}\n"
            f"{_details(name='b', args='{{}}', result='2')}"
        )
        msgs = segments_to_messages(parse_assistant_content(content))
        ids = [
            m["tool_calls"][0]["id"]
            for m in msgs
            if m.get("role") == "assistant" and m.get("tool_calls")
        ]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)

    def test_tool_only_no_empty_junk(self):
        content = _details()
        msgs = segments_to_messages(parse_assistant_content(content))
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "assistant")
        self.assertTrue(msgs[0].get("tool_calls"))
        self.assertEqual(msgs[1]["role"], "tool")
        for m in msgs:
            if m["role"] == "assistant" and not m.get("tool_calls"):
                self.assertTrue((m.get("content") or "").strip())

    def test_details_at_end(self):
        content = f"先說明一下\n{_details()}"
        msgs = segments_to_messages(parse_assistant_content(content))
        self.assertEqual(msgs[0]["role"], "assistant")
        self.assertIn("先說明", msgs[0].get("content") or "")
        self.assertTrue(msgs[0].get("tool_calls"))
        self.assertEqual(msgs[-1]["role"], "tool")

    def test_args_failure_becomes_empty_object(self):
        content = _details(args="NOT_JSON{{{")
        segs = parse_assistant_content(content)
        msgs = segments_to_messages(segs)
        args_str = msgs[0]["tool_calls"][0]["function"]["arguments"]
        parsed = json.loads(args_str)
        self.assertIsInstance(parsed, dict)

    def test_long_result_truncated(self):
        long_result = "X" * 500
        content = _details(result=long_result)
        segs = parse_assistant_content(content, max_result_length=50)
        msgs = segments_to_messages(segs)
        tool_msg = next(m for m in msgs if m["role"] == "tool")
        self.assertLessEqual(len(tool_msg["content"]), 60)


class TestConvertAndSanitize(unittest.TestCase):
    def test_already_has_tool_calls_unchanged(self):
        msg = {
            "role": "assistant",
            "content": "x",
            "tool_calls": [
                {
                    "id": "call_existing",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        }
        out = convert_assistant_message(msg)
        self.assertEqual(len(out), 1)
        self.assertIs(out[0], msg)

    def test_no_details_unchanged(self):
        msg = {"role": "assistant", "content": "hello"}
        out = convert_assistant_message(msg)
        self.assertEqual(len(out), 1)
        self.assertIs(out[0], msg)

    def test_mixed_conversation_order(self):
        messages = [
            {"role": "user", "content": "查天氣"},
            {"role": "assistant", "content": f"好\n{_details()}\n晴天喔"},
            {"role": "user", "content": "謝謝"},
        ]
        out = sanitize_messages_structured(
            messages,
            {
                "enable_history_sanitization": True,
                "sanitization_result_max_length": 20000,
            },
        )
        self.assertEqual(out[0]["role"], "user")
        self.assertEqual(out[0]["content"], "查天氣")
        self.assertEqual(out[-1]["role"], "user")
        self.assertEqual(out[-1]["content"], "謝謝")
        roles = [m["role"] for m in out]
        self.assertIn("tool", roles)
        for i, m in enumerate(out):
            if m.get("role") == "tool":
                prev = out[i - 1]
                self.assertEqual(prev["role"], "assistant")
                self.assertEqual(prev["tool_calls"][0]["id"], m["tool_call_id"])

    def test_sanitize_passthrough_when_no_tools(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        out = sanitize_messages_structured(messages, {})
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["content"], "hello")


if __name__ == "__main__":
    unittest.main()
