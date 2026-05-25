"""tmux command helpers for pocket-claude."""

from __future__ import annotations

import subprocess

CAPTURE_LINES = 50


def tmux_run(args: list[str]) -> tuple[bool, str]:
    """Run a tmux command and return (success, stdout_or_error)."""
    try:
        result = subprocess.run(
            ["tmux"] + args,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0, result.stdout.strip()
    except Exception as e:
        return False, str(e)


def list_sessions() -> list[str]:
    ok, out = tmux_run(["list-sessions", "-F", "#{session_name}"])
    if not ok or not out:
        return []
    return out.strip().split("\n")


def session_exists(name: str) -> bool:
    return name in list_sessions()


def send_keys(session: str, text: str):
    """Send text followed by Enter to a tmux session."""
    tmux_run(["send-keys", "-t", session, "--", text])
    tmux_run(["send-keys", "-t", session, "Enter"])


def send_ctrl_c(session: str):
    tmux_run(["send-keys", "-t", session, "C-c", ""])


def send_confirm(session: str, answer: str):
    """Send y/n confirmation without Enter."""
    tmux_run(["send-keys", "-t", session, answer, ""])


def capture_pane(session: str, lines: int = CAPTURE_LINES) -> str:
    """Capture recent pane contents from a tmux session."""
    ok, out = tmux_run([
        "capture-pane", "-t", session, "-p",
        "-S", f"-{lines}",
    ])
    return out if ok else ""
