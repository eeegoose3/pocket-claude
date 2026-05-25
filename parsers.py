"""Versioned parsers for Claude Code, Codex, and screen-based logs.

The bridge depends on JSONL formats owned by upstream CLI tools.  Keep those
format assumptions behind parser classes so compatibility can be tested and
updated independently from the Feishu/tmux runtime.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}


@dataclass(frozen=True)
class ParserInfo:
    """Human-readable parser metadata for doctor/debug/tests."""

    name: str
    agent: str
    format_version: str


class BaseLogParser:
    """Common no-op parser contract."""

    info = ParserInfo(name="base", agent="generic", format_version="unknown")

    def loads(self, line_str: str) -> dict[str, Any] | None:
        try:
            value = json.loads(line_str)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def session_id_from_log_path(self, path: str) -> str:
        base = os.path.basename(path).replace(".jsonl", "")
        return base

    def extract_user_text(self, line_str: str) -> str | None:
        return None

    def extract_assistant_text(self, line_str: str) -> str | None:
        return None

    def extract_interactive_ui(self, line_str: str) -> dict[str, Any] | None:
        return None

    def extract_image_write(self, line_str: str) -> dict[str, str] | None:
        return None

    def extract_screenshot_path(self, line_str: str, tool_id: str) -> str | None:
        return None

    def extract_system_event(self, line_str: str) -> dict[str, Any] | None:
        return None

    def check_tool_result(self, line_str: str, tool_id: str) -> bool:
        return False

    def is_turn_complete(self, line_str: str) -> bool:
        return False


class ScreenParser(BaseLogParser):
    """Fallback parser for generic CLIs where only tmux screen text exists."""

    info = ParserInfo(name="screen", agent="generic", format_version="tmux-screen-v1")


class ClaudeJsonlParser(BaseLogParser):
    """Claude Code JSONL parser.

    Current contract covered by fixtures:
    - top-level type: user / assistant / system
    - message.role and message.content for text/tool events
    - tool_use / tool_result correlation through id/tool_use_id
    """

    info = ParserInfo(name="claude-jsonl", agent="claude", format_version="claude-code-jsonl-v1")

    def extract_user_text(self, line_str: str) -> str | None:
        d = self.loads(line_str)
        if not d or d.get("type") != "user":
            return None

        msg = d.get("message", {})
        content = msg.get("content")

        if isinstance(content, str):
            return content.strip() if content.strip() else None

        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, str):
                    texts.append(item.strip())
                elif isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", "").strip())
            result = "\n".join(t for t in texts if t)
            return result if result else None

        return None

    def extract_assistant_text(self, line_str: str) -> str | None:
        d = self.loads(line_str)
        if not d:
            return None

        role = d.get("role") or d.get("message", {}).get("role")
        if role != "assistant":
            return None

        msg = d.get("message", d)
        content = msg.get("content", [])

        if isinstance(content, str):
            return content.strip() if content.strip() else None

        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text", "").strip():
                    texts.append(item["text"].strip())
            return "\n".join(texts) if texts else None

        return None

    def extract_interactive_ui(self, line_str: str) -> dict[str, Any] | None:
        d = self.loads(line_str)
        if not d:
            return None

        role = d.get("role") or d.get("message", {}).get("role")
        if role != "assistant":
            return None

        msg = d.get("message", d)
        content = msg.get("content", [])
        if not isinstance(content, list):
            return None

        results = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            name = item.get("name", "")
            inp = item.get("input", {})
            tool_id = item.get("id", "")

            if name == "AskUserQuestion":
                results.append({"type": "ask", "questions": inp.get("questions", [])})
            elif name == "ExitPlanMode":
                results.append({
                    "type": "plan_exit",
                    "plan": inp.get("plan"),
                    "allowed_prompts": inp.get("allowedPrompts", []),
                })
            elif name in ("Bash", "Edit", "Write", "NotebookEdit"):
                if name == "Bash":
                    detail = inp.get("description") or inp.get("command", "")[:120]
                elif name == "Edit":
                    detail = inp.get("file_path", "")
                else:
                    detail = inp.get("file_path", "") or inp.get("notebook_path", "")
                results.append({"type": "tool_pending", "name": name, "id": tool_id, "detail": detail})

        for result in results:
            if result["type"] in ("ask", "plan_exit"):
                return result
        return results[0] if results else None

    def extract_image_write(self, line_str: str) -> dict[str, str] | None:
        d = self.loads(line_str)
        if not d:
            return None

        role = d.get("role") or d.get("message", {}).get("role")
        if role != "assistant":
            return None

        msg = d.get("message", d)
        content = msg.get("content", [])
        if not isinstance(content, list):
            return None

        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_use":
                continue
            name = item.get("name", "")
            if name == "Write":
                path = item.get("input", {}).get("file_path", "")
                ext = os.path.splitext(path)[1].lower()
                if ext in IMAGE_EXTS:
                    return {"tool_id": item.get("id", ""), "path": path}
            elif name == "mcp__playwright__browser_take_screenshot":
                return {"tool_id": item.get("id", ""), "path": "__screenshot__"}
        return None

    def extract_screenshot_path(self, line_str: str, tool_id: str) -> str | None:
        d = self.loads(line_str)
        if not d or d.get("type") != "user":
            return None

        msg = d.get("message", d)
        content = msg.get("content", [])
        if not isinstance(content, list):
            return None

        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_result" and item.get("tool_use_id") == tool_id:
                result_content = item.get("content", "")
                if isinstance(result_content, str):
                    match = re.search(r"(/[^\s\"']+\.(?:png|jpg|jpeg|gif|webp))", result_content)
                    if match:
                        return match.group(1)
                elif isinstance(result_content, list):
                    for sub in result_content:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            match = re.search(r"(/[^\s\"']+\.(?:png|jpg|jpeg|gif|webp))", sub.get("text", ""))
                            if match:
                                return match.group(1)
        return None

    def extract_system_event(self, line_str: str) -> dict[str, Any] | None:
        d = self.loads(line_str)
        if not d or d.get("type") != "system":
            return None

        subtype = d.get("subtype", "")
        if subtype in ("compact_boundary", "microcompact_boundary"):
            meta = d.get("compactMetadata", {})
            return {"type": "compact", "pre_tokens": meta.get("preTokens", 0), "trigger": meta.get("trigger", "")}
        if subtype == "api_error":
            return {
                "type": "api_error",
                "retry_attempt": d.get("retryAttempt", 0),
                "max_retries": d.get("maxRetries", 10),
                "retry_in_ms": d.get("retryInMs", 0),
            }
        return None

    def check_tool_result(self, line_str: str, tool_id: str) -> bool:
        d = self.loads(line_str)
        if not d:
            return False

        if d.get("type") != "user":
            role = d.get("role") or d.get("message", {}).get("role")
            if role != "user":
                return False

        msg = d.get("message", d)
        content = msg.get("content", [])
        if not isinstance(content, list):
            return False

        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result" and item.get("tool_use_id") == tool_id:
                return True
        return False

    def is_turn_complete(self, line_str: str) -> bool:
        d = self.loads(line_str)
        if not d:
            return False

        role = d.get("role") or d.get("message", {}).get("role")
        if role != "assistant":
            return False

        msg = d.get("message", d)
        content = msg.get("content", [])
        if isinstance(content, str):
            return bool(content.strip())
        if not isinstance(content, list):
            return False
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                return False
        return True


class CodexJsonlParser(BaseLogParser):
    """Codex JSONL parser.

    Current contract covered by fixtures:
    - event_msg/user_message and event_msg/agent_message
    - event_msg/task_complete
    - response_item/function_call and response_item/function_call_output
    """

    info = ParserInfo(name="codex-jsonl", agent="codex", format_version="codex-rollout-jsonl-v1")

    def session_id_from_log_path(self, path: str) -> str:
        base = os.path.basename(path).replace(".jsonl", "")
        match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", base)
        return match.group(1) if match else base

    def extract_user_text(self, line_str: str) -> str | None:
        d = self.loads(line_str)
        if not d:
            return None
        payload = d.get("payload", {})
        if d.get("type") == "event_msg" and payload.get("type") == "user_message":
            text = payload.get("message", "")
            return text.strip() if isinstance(text, str) and text.strip() else None
        return None

    def extract_assistant_text(self, line_str: str) -> str | None:
        d = self.loads(line_str)
        if not d:
            return None
        payload = d.get("payload", {})
        if d.get("type") == "event_msg" and payload.get("type") == "agent_message":
            text = payload.get("message", "")
            return text.strip() if isinstance(text, str) and text.strip() else None
        return None

    def extract_interactive_ui(self, line_str: str) -> dict[str, Any] | None:
        d = self.loads(line_str)
        if not d:
            return None
        payload = d.get("payload", {})
        if d.get("type") != "response_item" or payload.get("type") != "function_call":
            return None

        name = payload.get("name", "")
        call_id = payload.get("call_id", "")
        args_raw = payload.get("arguments") or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except Exception:
            args = {}

        if name == "request_user_input":
            questions = args.get("questions", []) if isinstance(args, dict) else []
            return {"type": "ask", "questions": questions}

        if name in ("exec_command", "apply_patch", "write_stdin"):
            if isinstance(args, dict):
                detail = args.get("justification") or args.get("cmd") or args.get("session_id") or ""
                if args.get("sandbox_permissions") == "require_escalated":
                    return {"type": "tool_pending", "name": name, "id": call_id, "detail": str(detail)[:500]}
            return None
        return None

    def check_tool_result(self, line_str: str, tool_id: str) -> bool:
        d = self.loads(line_str)
        if not d:
            return False
        payload = d.get("payload", {})
        return d.get("type") == "response_item" and payload.get("type") == "function_call_output" and payload.get("call_id") == tool_id

    def is_turn_complete(self, line_str: str) -> bool:
        d = self.loads(line_str)
        if not d:
            return False
        payload = d.get("payload", {})
        return d.get("type") == "event_msg" and payload.get("type") == "task_complete"


PARSERS: dict[str, BaseLogParser] = {
    "claude": ClaudeJsonlParser(),
    "codex": CodexJsonlParser(),
    "generic": ScreenParser(),
    "screen": ScreenParser(),
}


def parser_for_agent(agent: str | None) -> BaseLogParser:
    """Return the parser for a known backend name."""
    return PARSERS.get((agent or "").lower(), ScreenParser())


def detect_parser(line_str: str, agent: str | None = None) -> BaseLogParser:
    """Pick a parser by explicit backend or by the JSONL line shape."""
    if agent:
        return parser_for_agent(agent)

    try:
        d = json.loads(line_str)
    except json.JSONDecodeError:
        return parser_for_agent("screen")
    if not isinstance(d, dict):
        return parser_for_agent("screen")

    payload = d.get("payload", {})
    if d.get("type") in ("event_msg", "response_item") or payload:
        return parser_for_agent("codex")
    if d.get("type") in ("user", "assistant", "system") or d.get("role") or d.get("message", {}).get("role"):
        return parser_for_agent("claude")
    return parser_for_agent("screen")


def parser_metadata() -> list[ParserInfo]:
    """Return parser metadata for tests/debugging."""
    return [PARSERS[name].info for name in ("claude", "codex", "screen")]


# Backward-compatible function API used by monitor/history/backends.
def session_id_from_log_path(path: str, agent: str | None = None) -> str:
    if agent:
        return parser_for_agent(agent).session_id_from_log_path(path)
    # Codex files look like rollout-2026-05-25T00-55-27-<uuid>.jsonl.
    return CodexJsonlParser().session_id_from_log_path(path)


def extract_user_text(line_str: str, agent: str | None = None) -> str | None:
    return detect_parser(line_str, agent).extract_user_text(line_str)


def extract_assistant_text(line_str: str, agent: str | None = None) -> str | None:
    return detect_parser(line_str, agent).extract_assistant_text(line_str)


def extract_interactive_ui(line_str: str, agent: str | None = None) -> dict[str, Any] | None:
    return detect_parser(line_str, agent).extract_interactive_ui(line_str)


def extract_image_write(line_str: str, agent: str | None = None) -> dict[str, str] | None:
    return detect_parser(line_str, agent).extract_image_write(line_str)


def extract_screenshot_path(line_str: str, tool_id: str, agent: str | None = None) -> str | None:
    return detect_parser(line_str, agent).extract_screenshot_path(line_str, tool_id)


def extract_system_event(line_str: str, agent: str | None = None) -> dict[str, Any] | None:
    return detect_parser(line_str, agent).extract_system_event(line_str)


def check_tool_result(line_str: str, tool_id: str, agent: str | None = None) -> bool:
    return detect_parser(line_str, agent).check_tool_result(line_str, tool_id)


def is_turn_complete(line_str: str, agent: str | None = None) -> bool:
    return detect_parser(line_str, agent).is_turn_complete(line_str)
