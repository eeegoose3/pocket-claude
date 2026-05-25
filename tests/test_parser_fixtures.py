from pathlib import Path
import unittest

import parsers


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def read_fixture(name):
    return (FIXTURE_DIR / name).read_text().splitlines()


class ParserFixtureTests(unittest.TestCase):
    def test_claude_fixture_contract(self):
        user, assistant, tool_use, tool_result, system_event = read_fixture("claude_sample.jsonl")
        parser = parsers.ClaudeJsonlParser()

        self.assertEqual(parser.extract_user_text(user), "hello claude")
        self.assertEqual(parser.extract_assistant_text(assistant), "hi from claude")
        self.assertTrue(parser.is_turn_complete(assistant))

        ui = parser.extract_interactive_ui(tool_use)
        self.assertEqual(ui["type"], "tool_pending")
        self.assertEqual(ui["name"], "Bash")
        self.assertEqual(ui["id"], "tool_1")
        self.assertTrue(parser.check_tool_result(tool_result, "tool_1"))

        event = parser.extract_system_event(system_event)
        self.assertEqual(event["type"], "compact")
        self.assertEqual(event["pre_tokens"], 123)

    def test_codex_fixture_contract(self):
        user, assistant, tool_use, tool_result, complete = read_fixture("codex_sample.jsonl")
        parser = parsers.CodexJsonlParser()

        self.assertEqual(parser.extract_user_text(user), "hello codex")
        self.assertEqual(parser.extract_assistant_text(assistant), "hi from codex")

        ui = parser.extract_interactive_ui(tool_use)
        self.assertEqual(ui["type"], "tool_pending")
        self.assertEqual(ui["name"], "exec_command")
        self.assertEqual(ui["id"], "call_1")
        self.assertIn("Need network", ui["detail"])

        self.assertTrue(parser.check_tool_result(tool_result, "call_1"))
        self.assertTrue(parser.is_turn_complete(complete))


if __name__ == "__main__":
    unittest.main()
