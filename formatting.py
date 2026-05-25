"""Text formatting helpers for Phone Agent Remote output."""

from __future__ import annotations

import re


SPINNER_CHARS_RE = re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◐◑◒◓⣾⣽⣻⢿⡿⣟⣯⣷]")
MARKDOWN_RE = re.compile(r"\|.+\|.+\||```|^\*\*.*\*\*|^#{1,4}\s|\[.+\]\(.+\)", re.MULTILINE)
TABLE_RE = re.compile(
    r"((?:^[ \t]*\|.+\|[ \t]*$\n?){3,})",
    re.MULTILINE,
)
TABLE_SEPARATOR_RE = re.compile(r"^\|?[\s\-:|]+(\|[\s\-:|]+)+\|?$")


def clean_ansi(text: str) -> str:
    """Clean ANSI escape sequences and CLI spinner symbols."""
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    text = re.sub(r"\x1b\].*?\x07", "", text)  # OSC sequences
    text = SPINNER_CHARS_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def has_markdown(text: str) -> bool:
    """Return whether text appears to contain Markdown formatting."""
    return bool(MARKDOWN_RE.search(text))


def markdown_table_to_vertical(table_text: str) -> str | None:
    """Convert a Markdown table into mobile-friendly vertical blocks.

    If the first column is a label column such as dimension/item/feature, each
    following column becomes one block. Otherwise each row becomes one block.
    Return None if the input does not look like a Markdown table.
    """
    lines = [line.strip() for line in table_text.strip().split("\n") if line.strip()]
    if len(lines) < 3:
        return None

    def split_row(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip("|").split("|")]

    headers = split_row(lines[0])
    sep = lines[1]
    if not TABLE_SEPARATOR_RE.match(sep):
        return None

    data_rows = [split_row(line) for line in lines[2:]]
    if not data_rows:
        return None

    label_keywords = {"维度", "项目", "指标", "功能", "对比", "特性", "属性", "feature", "dimension", ""}
    first_header_lower = headers[0].strip("*").lower() if headers else ""
    is_comparison = len(headers) >= 3 and first_header_lower in label_keywords

    parts: list[str] = []
    if is_comparison:
        for col_idx in range(1, len(headers)):
            block = f"**▎{headers[col_idx]}**"
            for row in data_rows:
                label = row[0] if len(row) > 0 else ""
                value = row[col_idx] if col_idx < len(row) else ""
                if label and value:
                    block += f"\n{label}：{value}"
            parts.append(block)
    else:
        for row in data_rows:
            block_lines = []
            for i, header in enumerate(headers):
                value = row[i] if i < len(row) else ""
                if value:
                    block_lines.append(f"{header}：{value}")
            if block_lines:
                first_val = row[0] if row else ""
                title = f"**▎{first_val}**" if first_val else ""
                remaining = [
                    f"{headers[i]}：{row[i]}"
                    for i in range(1, min(len(headers), len(row)))
                    if row[i]
                ]
                if title:
                    parts.append(title + "\n" + "\n".join(remaining))
                else:
                    parts.append("\n".join(block_lines))

    return "\n\n".join(parts)


def convert_tables_in_text(text: str) -> str:
    """Replace Markdown tables in text with vertical blocks where possible."""

    def replace_table(match: re.Match[str]) -> str:
        vertical = markdown_table_to_vertical(match.group(1))
        return vertical if vertical else match.group(1)

    return TABLE_RE.sub(replace_table, text)
