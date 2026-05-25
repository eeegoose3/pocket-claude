import json
import unittest

import parsers


class ParserTests(unittest.TestCase):
    def test_codex_user_agent_and_complete(self):
        user = json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}})
        agent = json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "hi"}})
        complete = json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}})

        self.assertEqual(parsers.extract_user_text(user), "hello")
        self.assertEqual(parsers.extract_assistant_text(agent), "hi")
        self.assertTrue(parsers.is_turn_complete(complete))

    def test_codex_escalated_exec_detection(self):
        line = json.dumps({
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call_1",
                "arguments": json.dumps({
                    "cmd": "git fetch",
                    "sandbox_permissions": "require_escalated",
                    "justification": "Need network",
                }),
            },
        })
        ui = parsers.extract_interactive_ui(line)
        self.assertEqual(ui["type"], "tool_pending")
        self.assertEqual(ui["name"], "exec_command")
        self.assertEqual(ui["id"], "call_1")
        self.assertIn("Need network", ui["detail"])

    def test_claude_text_and_tool_result(self):
        assistant = json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
            },
        })
        tool_result = json.dumps({
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "tool_1", "content": "ok"}],
            },
        })
        self.assertEqual(parsers.extract_assistant_text(assistant), "done")
        self.assertTrue(parsers.check_tool_result(tool_result, "tool_1"))

    def test_session_id_from_codex_path(self):
        path = "/x/rollout-2026-05-25T00-55-27-019e5e21-b1a3-75c2-8521-5391b4ff644b.jsonl"
        self.assertEqual(
            parsers.session_id_from_log_path(path),
            "019e5e21-b1a3-75c2-8521-5391b4ff644b",
        )


if __name__ == "__main__":
    unittest.main()
