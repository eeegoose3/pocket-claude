"""Runtime helpers for tmux sessions, backend inference, and caffeinate."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import subprocess
import time

from backends import (
    backend_display as backend_display_for_agent,
    infer_backend_from_command,
    normalize_agent as normalize_agent_value,
)
from tmux import send_keys as tmux_send_keys, tmux_run

log = logging.getLogger("bridge")

caffeinate_proc = None


@dataclass
class SessionRuntimeContext:
    default_agent: str
    session_backend: dict[str, str]
    bridge_sent_time: dict[str, float]


def normalize_agent(agent: str | None, default_agent: str) -> str:
    return normalize_agent_value(agent, default_agent)


def get_backend(session_name: str | None, ctx: SessionRuntimeContext) -> str:
    """Return backend for a tmux session, inferring from the running pane if needed."""
    if session_name and session_name in ctx.session_backend:
        return normalize_agent(ctx.session_backend[session_name], ctx.default_agent)

    inferred = None
    if session_name:
        ok, pane_cmd = tmux_run(["display-message", "-t", session_name, "-p", "#{pane_current_command}"])
        if ok:
            inferred = infer_backend_from_command(pane_cmd, ctx.default_agent)
    backend = normalize_agent(inferred or ctx.default_agent, ctx.default_agent)
    if session_name:
        ctx.session_backend[session_name] = backend
    return backend


def backend_display(session_name: str | None, ctx: SessionRuntimeContext) -> str:
    return backend_display_for_agent(get_backend(session_name, ctx))


def send_keys(session: str, text: str, ctx: SessionRuntimeContext) -> None:
    """Send text to a tmux session and record bridge sent time."""
    ctx.bridge_sent_time[session] = time.time()
    tmux_send_keys(session, text)


def create_tmux_and_run(session_name: str, command: str) -> tuple[bool, str]:
    """Create a tmux session and run a command inside it."""
    ok, _ = tmux_run(["new-session", "-d", "-s", session_name])
    if not ok:
        return False, f"创建 tmux session '{session_name}' 失败（可能已存在）"
    tmux_run(["send-keys", "-t", session_name, "--", command])
    tmux_run(["send-keys", "-t", session_name, "Enter"])
    return True, ""


def start_caffeinate() -> None:
    """Start caffeinate to prevent system sleep."""
    global caffeinate_proc
    try:
        caffeinate_proc = subprocess.Popen(
            ["caffeinate", "-s"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info(f"caffeinate 已启动 (PID {caffeinate_proc.pid})，Mac 不会自动睡眠")
    except Exception as e:
        log.error(f"caffeinate 启动失败: {e}")


def stop_caffeinate() -> None:
    """Stop caffeinate."""
    global caffeinate_proc
    if caffeinate_proc:
        caffeinate_proc.terminate()
        caffeinate_proc.wait()
        log.info("caffeinate 已停止")
        caffeinate_proc = None


def is_caffeinate_running() -> bool:
    return bool(caffeinate_proc and caffeinate_proc.poll() is None)
