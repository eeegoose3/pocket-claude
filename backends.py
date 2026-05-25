"""Backend helpers for Claude Code, Codex, and generic CLI sessions."""

from __future__ import annotations

import glob
import json
import os
import re
import shlex

from parsers import session_id_from_log_path

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")

BACKENDS = {
    "claude": {
        "display": "Claude Code",
        "binary": "claude",
        "log_root": CLAUDE_PROJECTS_DIR,
        "resume_arg": "--resume",
    },
    "codex": {
        "display": "Codex",
        "binary": "codex",
        "log_root": CODEX_SESSIONS_DIR,
        "resume_arg": "resume",
    },
    "generic": {
        "display": "Generic CLI",
        "binary": None,
        "log_root": None,
        "resume_arg": None,
    },
}

AGENT_ALIASES = {
    "claude": "claude",
    "cc": "claude",
    "claude-code": "claude",
    "codex": "codex",
    "openai": "codex",
    "generic": "generic",
    "cli": "generic",
}


def normalize_agent(agent: str | None, default: str = "claude") -> str:
    """Normalize user/backend names to claude/codex/generic."""
    if not agent:
        agent = default
    key = str(agent).strip().lower()
    return AGENT_ALIASES.get(key, key if key in BACKENDS else "generic")


def infer_backend_from_command(pane_cmd: str | None, default: str = "claude") -> str:
    """Infer backend from tmux pane_current_command."""
    cmd = (pane_cmd or "").lower()
    if "codex" in cmd:
        return "codex"
    if "claude" in cmd:
        return "claude"
    return normalize_agent(default)


def backend_display(agent: str | None) -> str:
    agent = normalize_agent(agent)
    return BACKENDS.get(agent, BACKENDS["generic"])["display"]


def start_command(agent: str, directory: str) -> str:
    """Shell command to start the selected CLI in a directory."""
    agent = normalize_agent(agent)
    binary = BACKENDS[agent]["binary"]
    if not binary:
        binary = os.getenv("GENERIC_CLI_COMMAND", "$SHELL")
    return f"cd {shlex.quote(directory)} && {binary}"


def resume_command(agent: str, cwd: str, session_id: str) -> str:
    """Shell command to resume an existing agent session."""
    agent = normalize_agent(agent)
    if agent == "codex":
        return f"cd {shlex.quote(cwd)} && codex resume {shlex.quote(session_id)}"
    if agent == "claude":
        return f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(session_id)}"
    return f"cd {shlex.quote(cwd)} && {os.getenv('GENERIC_CLI_COMMAND', '$SHELL')}"


def find_log_by_session_id(session_id, agent: str | None = None):
    """Find the JSONL log file for a Claude/Codex session id."""
    agent = normalize_agent(agent)
    roots = []
    if agent in ("claude", "generic"):
        roots.append(CLAUDE_PROJECTS_DIR)
    if agent in ("codex", "generic"):
        roots.append(CODEX_SESSIONS_DIR)
    matches = []
    for root in roots:
        if root and os.path.isdir(root):
            matches.extend(glob.glob(os.path.join(root, "**", f"*{session_id}*.jsonl"), recursive=True))
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def find_cwd_for_session_id(session_id, agent: str | None = None):
    """根据 Claude/Codex session-id 查找对应的项目目录"""
    match = find_log_by_session_id(session_id, agent)
    if not match:
        return None
    # JSONL 第一行可能是 snapshot 元数据，cwd 在后面几行，扫描前 20 行
    try:
        with open(match, "r") as f:
            for i, line in enumerate(f):
                if i >= 20:
                    break
                d = json.loads(line)
                cwd = d.get("cwd") or d.get("payload", {}).get("cwd")
                if not cwd and d.get("type") == "session_meta":
                    cwd = d.get("payload", {}).get("cwd")
                if cwd:
                    return cwd
    except Exception:
        pass
    return None


def _claude_project_dir_for_cwd(cwd):
    """Claude Code 的项目目录命名规则：路径中的 / _ 空格 都变成 -"""
    project_key = re.sub(r"[/_\s]", "-", cwd)
    project_dir = os.path.join(CLAUDE_PROJECTS_DIR, project_key)
    if os.path.isdir(project_dir):
        return project_dir
    basename = os.path.basename(cwd)
    if os.path.isdir(CLAUDE_PROJECTS_DIR):
        for d in os.listdir(CLAUDE_PROJECTS_DIR):
            if basename in d and os.path.isdir(os.path.join(CLAUDE_PROJECTS_DIR, d)):
                return os.path.join(CLAUDE_PROJECTS_DIR, d)
    return None


def _codex_jsonl_candidates(cwd=None):
    """Codex stores sessions under ~/.codex/sessions/YYYY/MM/DD/*.jsonl."""
    if not os.path.isdir(CODEX_SESSIONS_DIR):
        return []
    files = glob.glob(os.path.join(CODEX_SESSIONS_DIR, "**", "*.jsonl"), recursive=True)
    if not cwd:
        return files
    matched = []
    for fpath in files:
        try:
            with open(fpath, "r") as f:
                for i, line in enumerate(f):
                    if i >= 5:
                        break
                    d = json.loads(line)
                    if d.get("type") == "session_meta" and d.get("payload", {}).get("cwd") == cwd:
                        matched.append(fpath)
                        break
        except Exception:
            continue
    return matched



def jsonl_candidates_for_agent(agent: str, cwd: str | None = None):
    """Return candidate JSONL logs for an agent/cwd pair."""
    agent = normalize_agent(agent)
    if agent == "claude":
        if not cwd:
            return []
        project_dir = _claude_project_dir_for_cwd(cwd)
        return glob.glob(os.path.join(project_dir, "*.jsonl")) if project_dir else []
    if agent == "codex":
        return _codex_jsonl_candidates(cwd)
    return []
