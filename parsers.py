"""Pure JSONL parsers for Claude Code and Codex logs.

This module intentionally has no Feishu/tmux/runtime state dependency so parser
compatibility can be tested independently from the bridge process.
"""

from __future__ import annotations

import json
import os
import re

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}


def session_id_from_log_path(path, agent: str | None = None):
    """Extract Claude/Codex session id from a JSONL file path."""
    base = os.path.basename(path).replace(".jsonl", "")
    # Codex files look like rollout-2026-05-25T00-55-27-<uuid>.jsonl
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", base)
    return m.group(1) if m else base


def extract_user_text(line_str):
    """从 Claude/Codex JSONL 的一行中提取 user 消息文本"""
    try:
        d = json.loads(line_str)
    except json.JSONDecodeError:
        return None

    # Codex event message: {"type":"event_msg","payload":{"type":"user_message","message":"..."}}
    payload = d.get("payload", {})
    if d.get("type") == "event_msg" and payload.get("type") == "user_message":
        text = payload.get("message", "")
        return text.strip() if isinstance(text, str) and text.strip() else None

    # user 消息的 type 字段为 "user"
    if d.get("type") != "user":
        return None

    msg = d.get("message", {})
    content = msg.get("content")

    if isinstance(content, str):
        return content.strip() if content.strip() else None

    # content 也可能是列表（和 assistant 类似）
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


def extract_assistant_text(line_str):
    """从 Claude/Codex JSONL 的一行中提取 assistant 文本回复。

    Claude Code uses message.content text/tool_use entries.
    Codex TUI writes display-ready agent messages as event_msg/agent_message.
    """
    try:
        d = json.loads(line_str)
    except json.JSONDecodeError:
        return None

    payload = d.get("payload", {})
    if d.get("type") == "event_msg" and payload.get("type") == "agent_message":
        text = payload.get("message", "")
        return text.strip() if isinstance(text, str) and text.strip() else None

    # 只处理 assistant 消息
    role = d.get("role") or d.get("message", {}).get("role")
    if role != "assistant":
        return None

    # 内容在 message.content 里
    msg = d.get("message", d)
    content = msg.get("content", [])

    if isinstance(content, str):
        return content.strip() if content.strip() else None

    if isinstance(content, list):
        texts = []
        for item in content:
            if item.get("type") == "text" and item.get("text", "").strip():
                texts.append(item["text"].strip())
        return "\n".join(texts) if texts else None

    return None


def extract_interactive_ui(line_str):
    """从 Claude/Codex JSONL 行中检测交互式 UI / 权限事件。
    比屏幕检测更早触发：JSONL 写入在终端渲染之前。
    返回值类型：
      {"type": "ask", "questions": [...]}           — AskUserQuestion 选择菜单
      {"type": "plan_exit", "plan": "...", ...}     — ExitPlanMode 计划确认
      {"type": "tool_pending", "name": "Bash", "id": "...", "detail": "..."}  — 可能需要权限确认的工具调用
      None — 无交互
    """
    try:
        d = json.loads(line_str)
    except json.JSONDecodeError:
        return None

    # Codex: tool calls are response_item/function_call. Escalated exec calls
    # usually produce a TUI approval prompt; notify the phone as soon as seen.
    payload = d.get("payload", {})
    if d.get("type") == "response_item" and payload.get("type") == "function_call":
        name = payload.get("name", "")
        call_id = payload.get("call_id", "")
        args_raw = payload.get("arguments") or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except Exception:
            args = {}

        if name == "request_user_input":
            questions = args.get("questions", [])
            return {"type": "ask", "questions": questions}

        if name in ("exec_command", "apply_patch", "write_stdin"):
            detail = ""
            if isinstance(args, dict):
                detail = args.get("justification") or args.get("cmd") or args.get("session_id") or ""
                if args.get("sandbox_permissions") == "require_escalated":
                    return {"type": "tool_pending", "name": name, "id": call_id, "detail": str(detail)[:500]}
            # For non-escalated Codex calls we do not notify as permission prompts.
            return None

    role = d.get("role") or d.get("message", {}).get("role")
    if role != "assistant":
        return None

    msg = d.get("message", d)
    content = msg.get("content", [])
    if not isinstance(content, list):
        return None

    # 收集所有 tool_use（一条 assistant 消息可能包含多个）
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
            # 可能需要权限确认的工具调用
            if name == "Bash":
                detail = inp.get("description") or inp.get("command", "")[:120]
            elif name == "Edit":
                detail = inp.get("file_path", "")
            else:
                detail = inp.get("file_path", "") or inp.get("notebook_path", "")
            results.append({"type": "tool_pending", "name": name, "id": tool_id, "detail": detail})

    # 优先返回交互式 UI，其次返回工具调用
    for r in results:
        if r["type"] in ("ask", "plan_exit"):
            return r
    return results[0] if results else None


def extract_image_write(line_str):
    """检测 assistant 消息中是否有 Write 工具写入图片文件。
    返回 {"tool_id": str, "path": str} 或 None。
    """
    try:
        d = json.loads(line_str)
    except json.JSONDecodeError:
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


def extract_screenshot_path(line_str, tool_id):
    """从 Playwright 截图的 tool_result 中提取文件路径。"""
    try:
        d = json.loads(line_str)
    except json.JSONDecodeError:
        return None

    if d.get("type") != "user":
        return None

    msg = d.get("message", d)
    content = msg.get("content", [])
    if not isinstance(content, list):
        return None

    for item in content:
        if item.get("type") == "tool_result" and item.get("tool_use_id") == tool_id:
            # Playwright 截图结果中可能包含文件路径
            result_content = item.get("content", "")
            if isinstance(result_content, str):
                # 搜索文件路径模式
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


def extract_system_event(line_str):
    """从 JSONL 行中检测系统事件（上下文压缩、API 错误等）。
    返回 {"type": "compact", "pre_tokens": N} 或 {"type": "api_error", ...} 或 None
    """
    try:
        d = json.loads(line_str)
    except json.JSONDecodeError:
        return None

    if d.get("type") != "system":
        return None

    subtype = d.get("subtype", "")
    if subtype in ("compact_boundary", "microcompact_boundary"):
        meta = d.get("compactMetadata", {})
        return {"type": "compact", "pre_tokens": meta.get("preTokens", 0), "trigger": meta.get("trigger", "")}
    elif subtype == "api_error":
        return {
            "type": "api_error",
            "retry_attempt": d.get("retryAttempt", 0),
            "max_retries": d.get("maxRetries", 10),
            "retry_in_ms": d.get("retryInMs", 0),
        }

    return None


def check_tool_result(line_str, tool_id):
    """检查 JSONL 行是否包含指定 tool_id/call_id 的结果（表示权限已确认或工具已执行）"""
    try:
        d = json.loads(line_str)
    except json.JSONDecodeError:
        return False

    payload = d.get("payload", {})
    if d.get("type") == "response_item" and payload.get("type") == "function_call_output":
        return payload.get("call_id") == tool_id

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


def is_turn_complete(line_str):
    """检测是否为一轮回复结束。"""
    try:
        d = json.loads(line_str)
    except json.JSONDecodeError:
        return False
    payload = d.get("payload", {})
    if d.get("type") == "event_msg" and payload.get("type") == "task_complete":
        return True
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
            return False  # 还有工具要执行，不算说完
    return True
