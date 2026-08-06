"""Wiring tests: structured-only sanitize + responses helper."""

from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "/home/thomas2018/hermes_tool_filter")

import main as M
from responses_handler import build_previous_tool_context
import tool_history_format as THF


DETAILS = (
    '<details type="tool_calls" name="terminal">\n'
    "<summary>terminal</summary>\n"
    '<arguments>{"command":"uptime"}</arguments>\n'
    "<result>up 10 days</result>\n"
    "</details>"
)


class TestNoFlatExports(unittest.TestCase):
    def test_flat_helpers_removed(self):
        for name in (
            "flatten_json",
            "format_tool_history_block",
            "format_tool_history_legacy",
            "sanitize_message_content",
            "_format_args_flat",
            "_format_result_flat",
        ):
            self.assertFalse(hasattr(THF, name), name)

    def test_config_always_structured(self):
        enabled, max_len, fmt = THF._get_sanitization_config({})
        self.assertTrue(enabled)
        self.assertEqual(fmt, "structured")


class TestSanitizeWiring(unittest.TestCase):
    def setUp(self):
        self._old = dict(M.CONFIG)

    def tearDown(self):
        M.CONFIG.clear()
        M.CONFIG.update(self._old)

    def test_structured_expands_and_no_hint(self):
        M.CONFIG["enable_history_sanitization"] = True
        M.CONFIG["sanitization_result_max_length"] = 20000
        # leftover config keys must be ignored
        M.CONFIG["tool_usage_hint_file"] = "tool_hint.txt"
        M.CONFIG["tool_history_format"] = "flat"

        msgs = [
            {"role": "user", "content": "查 uptime"},
            {"role": "assistant", "content": f"好\n{DETAILS}\n完成"},
            {"role": "user", "content": "繼續"},
        ]
        out = M.sanitize_request_messages([dict(m) for m in msgs])
        roles = [m["role"] for m in out]
        self.assertIn("tool", roles)
        last_user = [m for m in out if m["role"] == "user"][-1]
        self.assertEqual(last_user["content"], "繼續")
        blob = str(out)
        self.assertNotIn("START_PREV_ACTION", blob)
        self.assertNotIn("<tool_call>", last_user["content"])

        for i, m in enumerate(out):
            if m.get("role") == "tool":
                self.assertEqual(out[i - 1]["role"], "assistant")
                self.assertEqual(out[i - 1]["tool_calls"][0]["id"], m["tool_call_id"])

    def test_disabled_passthrough(self):
        M.CONFIG["enable_history_sanitization"] = False
        msgs = [
            {"role": "assistant", "content": f"好\n{DETAILS}\n完成"},
        ]
        out = M.sanitize_request_messages([dict(m) for m in msgs])
        self.assertEqual(len(out), 1)
        self.assertIn("<details", out[0]["content"])


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
        payload, kind = build_previous_tool_context(items, max_result_length=100)
        self.assertEqual(kind, "messages")
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["tool_calls"][0]["id"], "call_abc")
        self.assertEqual(payload[1]["tool_call_id"], "call_abc")
        self.assertEqual(payload[1]["content"], "晴天")

    def test_no_fmt_kwarg(self):
        # API no longer accepts fmt=
        import inspect
        sig = inspect.signature(build_previous_tool_context)
        self.assertNotIn("fmt", sig.parameters)


if __name__ == "__main__":
    unittest.main()
