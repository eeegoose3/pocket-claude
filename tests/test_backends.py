import os
import unittest

import backends


class BackendTests(unittest.TestCase):
    def test_normalize_aliases(self):
        self.assertEqual(backends.normalize_agent("cc"), "claude")
        self.assertEqual(backends.normalize_agent("openai"), "codex")
        self.assertEqual(backends.normalize_agent("unknown"), "generic")

    def test_infer_backend_from_command(self):
        self.assertEqual(backends.infer_backend_from_command("codex"), "codex")
        self.assertEqual(backends.infer_backend_from_command("claude"), "claude")
        self.assertEqual(backends.infer_backend_from_command("zsh", default="codex"), "codex")

    def test_commands_quote_paths_and_ids(self):
        self.assertEqual(
            backends.start_command("codex", "/tmp/a b"),
            "cd '/tmp/a b' && codex",
        )
        self.assertEqual(
            backends.resume_command("codex", "/tmp/a b", "abc def"),
            "cd '/tmp/a b' && codex resume 'abc def'",
        )

    def test_backend_display(self):
        self.assertEqual(backends.backend_display("codex"), "Codex")
        self.assertEqual(backends.backend_display("claude"), "Claude Code")


if __name__ == "__main__":
    unittest.main()
