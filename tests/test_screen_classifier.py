import unittest

from screen_classifier import classify_screen_input, looks_like_shell_command


class ScreenClassifierTests(unittest.TestCase):
    def test_detects_codex_prompt(self):
        screen = """╭────────────────╮
│ >_ OpenAI Codex │
╰────────────────╯

› Explain this codebase

gpt-5.5 high · ~/funny
"""
        target = classify_screen_input(screen)
        self.assertEqual(target.kind, "codex")

    def test_detects_shell_prompt(self):
        screen = "chouduck@MacBook-Air pocket-claude %"
        target = classify_screen_input(screen)
        self.assertEqual(target.kind, "shell")

    def test_shell_command_detection_avoids_natural_language(self):
        self.assertTrue(looks_like_shell_command("codex"))
        self.assertTrue(looks_like_shell_command("git status"))
        self.assertFalse(looks_like_shell_command("帮我看看项目"))
        self.assertFalse(looks_like_shell_command("help me inspect this project"))


if __name__ == "__main__":
    unittest.main()
