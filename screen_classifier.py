"""Classify the current tmux screen by looking at the visible input prompt.

This intentionally avoids process-name guesses.  The goal is to answer the
user-facing question: "if I type now, where will the input likely go?"
"""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ScreenInputTarget:
    kind: str
    label: str
    confidence: str = "medium"


CODEX_STATUS_RE = re.compile(r"\b(gpt-[\w.-]+|o[134](?:-[\w.-]+)?)\b.*[·•].*~?[/\w.-]+", re.I)
SHELL_PROMPT_RE = re.compile(
    r"(^|\n)\s*(?:[\w.-]+@[^\n%$#>]+\s+)?(?:~|/|[\w.-]+)[^\n]*\s[%$#>]\s*$"
)


def _visible_lines(screen_text: str) -> list[str]:
    return [line.rstrip() for line in screen_text.splitlines() if line.strip()]


def classify_screen_input(screen_text: str) -> ScreenInputTarget:
    """Classify where typing would likely go based on the visible prompt."""
    lines = _visible_lines(screen_text)
    if not lines:
        return ScreenInputTarget("unknown", "不确定", "low")

    tail_text = "\n".join(lines[-20:])
    last = lines[-1].strip()
    lower_tail = tail_text.lower()

    if "enter to select" in lower_tail or "❯" in tail_text:
        return ScreenInputTarget("menu", "交互菜单", "high")

    # Codex TUI commonly shows a `›` input prompt and/or a bottom model line
    # such as `gpt-5.5 high · ~/funny`.
    if last.startswith("›") or "openai codex" in lower_tail or CODEX_STATUS_RE.search(last):
        return ScreenInputTarget("codex", "Codex", "high")

    # Claude Code screens usually contain the Claude brand and often use a
    # similar input prompt. Prefer explicit Claude text over the generic prompt.
    if "claude code" in lower_tail or " claude " in f" {lower_tail} ":
        return ScreenInputTarget("claude", "Claude Code", "medium")

    # zsh/bash/fish prompts usually end the current input line with %, $, # or >.
    if SHELL_PROMPT_RE.search(tail_text) or re.search(r"[%$#>]\s*$", last):
        return ScreenInputTarget("shell", "Shell", "high")

    return ScreenInputTarget("unknown", "不确定", "low")


SHELL_COMMAND_START_RE = re.compile(
    r"^(?:"
    r"cd\b|ls\b|pwd\b|git\b|gh\b|codex\b|claude\b|python\b|python3\b|"
    r"node\b|npm\b|pnpm\b|yarn\b|uv\b|pip\b|venv/|cat\b|grep\b|rg\b|"
    r"echo\b|mkdir\b|touch\b|open\b|tmux\b|brew\b|curl\b|wget\b|ssh\b|"
    r"sudo\b|chmod\b|chown\b|find\b|sed\b|awk\b|make\b|pytest\b|./|../|~/|/|[A-Za-z_][A-Za-z0-9_]*="
    r")"
)


def looks_like_shell_command(text: str) -> bool:
    """Best-effort check for whether user text is intended as shell input."""
    stripped = text.strip()
    if not stripped:
        return False
    if "\n" in stripped:
        return True
    if stripped.startswith(("/", "./", "../", "~", "$")):
        return True
    # CJK/natural-language punctuation strongly suggests this is not a shell command.
    if re.search(r"[\u4e00-\u9fff]|[。？！？，、]", stripped):
        return False
    return bool(SHELL_COMMAND_START_RE.match(stripped))
