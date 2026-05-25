import unittest

from formatting import (
    clean_ansi,
    convert_tables_in_text,
    has_markdown,
    markdown_table_to_vertical,
)


class FormattingTests(unittest.TestCase):
    def test_clean_ansi_removes_escape_and_spinner(self):
        text = "\x1b[31mhello\x1b[0m ⠋\n\n\nworld"
        self.assertEqual(clean_ansi(text), "hello \n\nworld")

    def test_has_markdown_detects_common_patterns(self):
        self.assertTrue(has_markdown("# Title"))
        self.assertTrue(has_markdown("| A | B |\n|---|---|\n| x | y |"))
        self.assertFalse(has_markdown("plain text only"))

    def test_markdown_comparison_table_to_vertical(self):
        table = """| 维度 | A | B |
|---|---|---|
| 速度 | 快 | 慢 |
| 成本 | 低 | 高 |
"""
        self.assertEqual(
            markdown_table_to_vertical(table),
            "**▎A**\n速度：快\n成本：低\n\n**▎B**\n速度：慢\n成本：高",
        )

    def test_convert_tables_in_text_preserves_surrounding_text(self):
        text = "before\n| Name | Value |\n|---|---|\n| foo | bar |\nafter"
        converted = convert_tables_in_text(text)
        self.assertIn("before", converted)
        self.assertIn("**▎foo**", converted)
        self.assertIn("Value：bar", converted)
        self.assertIn("after", converted)

    def test_invalid_table_returns_none(self):
        self.assertIsNone(markdown_table_to_vertical("| A | B |\n| bad | sep |\n| x | y |"))


if __name__ == "__main__":
    unittest.main()
