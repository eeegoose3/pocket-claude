"""Conversation history loading from Claude/Codex JSONL logs."""

from __future__ import annotations

import logging

from backends import find_log_by_session_id
from parsers import extract_assistant_text, extract_user_text

log = logging.getLogger("bridge")


def load_recent_history(session_id: str, rounds: int = 3, agent: str | None = None) -> list[dict[str, str]]:
    """Load recent user/assistant rounds from an agent JSONL session log."""
    jsonl_path = find_log_by_session_id(session_id, agent)
    if not jsonl_path:
        log.warning(f"load_recent_history: 找不到 {session_id}.jsonl")
        return []

    messages = []  # [(index, role, text), ...]
    try:
        with open(jsonl_path, "r") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                user_text = extract_user_text(line)
                if user_text:
                    messages.append((i, "user", user_text))
                    continue
                assistant_text = extract_assistant_text(line)
                if assistant_text:
                    messages.append((i, "assistant", assistant_text))
    except Exception as e:
        log.error(f"load_recent_history: 读取 JSONL 失败: {e}")
        return []

    if not messages:
        return []

    rounds_collected = []  # [(user_msg, assistant_msg), ...]
    idx = len(messages) - 1
    while idx >= 0 and len(rounds_collected) < rounds:
        while idx >= 0 and messages[idx][1] != "assistant":
            idx -= 1
        if idx < 0:
            break
        assistant_msg = messages[idx]
        idx -= 1

        while idx >= 0 and messages[idx][1] != "user":
            idx -= 1
        if idx < 0:
            break
        user_msg = messages[idx]
        idx -= 1
        rounds_collected.append((user_msg, assistant_msg))

    rounds_collected.reverse()
    result = []
    for user_msg, assistant_msg in rounds_collected:
        result.append({"role": "user", "text": user_msg[2]})
        result.append({"role": "assistant", "text": assistant_msg[2]})
    return result
