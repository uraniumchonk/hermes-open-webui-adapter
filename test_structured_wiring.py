"""Wiring tests: main.sanitize_request_messages structured/flat branches + responses helper."""

from __future__ import annotations

import json
import sys
import unittest

sys.path.insert(0, "/home/thomas2018/hermes_tool_filter")

import main as M
from responses_handler import build_previous_tool_context


DETAILS = (
    '<details type="tool_calls" name="terminal">\n'
    "<summary>terminal</summary>\n"
    '<arguments>{"command":"uptime"}</arguments>\n'
    "<result>up 10 days</result>\n"
    "</details>"
)


class TestSanitizeWiring(unittest.TestCase):
    def setUp(self):
        self._old = dict(M.CONFIG)

    def tearDown(self):
        M.CONFIG.clear()
        M.CONFIG.update(self._old)

    def test_structured_expands_and_skips_hint(self):
        M.CONFIG["enable_history_sanitization"] = True
        M.CONFIG["tool_history_format"] = "structured"
        M.CONFIG["sanitization_result_max_length"] = 20000
        M.CONFIG["tool_usage_hint_file"] = "tool_hint.txt"

        msgs = [
            {"role": "user", "content": "查 uptime"},
            {"role": "assistant", "content": f"好\n{DETAILS}\n完成"},
            {"role": "user", "content": "繼續"},
        ]
        out = M.sanitize_request_messages([dict(m) for m in msgs])
        roles = [m["role"] for m in out]
        self.assertIn("tool", roles)
        # last user should NOT get tool_hint appended
        last_user = [m for m in out if m["role"] == "user"][-1]
        self.assertEqual(last_user["content"], "繼續")
        self.assertNotIn("<tool_call>", last_user["content"])
        self.assertNotIn("START_PREV_ACTION", last_user["content"])

        # pairing
        for i, m in enumerate(out):
            if m.get("role") == "tool":
                self.assertEqual(out[i - 1]["role"], "assistant")
                self.assertEqual(out[i - 1]["tool_calls"][0]["id"], m["tool_call_id"])

    def test_flat_still_inline_and_hint(self):
        M.CONFIG["enable_history_sanitization"] = True
        M.CONFIG["tool_history_format"] = "flat"
        M.CONFIG["sanitization_result_max_length"] = 20000
        M.CONFIG["tool_usage_hint_file"] = "tool_hint.txt"

        msgs = [
            {"role": "user", "content": "查 uptime"},
            {"role": "assistant", "content": f"好\n{DETAILS}\n完成"},
        ]
        out = M.sanitize_request_messages([dict(m) for m in msgs])
        self.assertFalse(any(m.get("role") == "tool" for m in out))
        asst = next(m for m in out if m["role"] == "assistant")
        self.assertIn("[START_PREV_ACTION]", asst["content"])
        # hint appended to last user
        user = next(m for m in out if m["role"] == "user")
        self.assertIn("<tool_call>", user["content"])


class TestResponsesBuildContext(unittest.TestCase):
    def test_structured_messages(self):
        items = [
            {
                "type": "function_call",
                "id": "call_abc",
                "name": "web_search",
                "arguments": '{"query":"天氣"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_abc",
                "output": "晴天",
            },
        ]
        payload, kind = build_previous_tool_context(items, fmt="structured", max_result_length=100)
        self.assertEqual(kind, "messages")
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["tool_calls"][0]["id"], "call_abc")
        self.assertEqual(payload[1]["tool_call_id"], "call_abc")
        self.assertEqual(payload[1]["content"], "晴天")

    def test_flat_text_blocks(self):
        items = [
            {
                "type": "function_call",
                "call_id": "c1",
                "id": "c1",
                "name": "terminal",
                "arguments": '{"command":"ls"}',
            },
            {"type": "function_call_output", "call_id": "c1", "output": "a.txt"},
        ]
        payload, kind = build_previous_tool_context(items, fmt="flat", max_result_length=100)
        self.assertEqual(kind, "text_blocks")
        self.assertTrue(payload[0].startswith("[START_PREV_ACTION]"))
        self.assertIn("terminal", payload[0])


if __name__ == "__main__":
    unittest.main()
