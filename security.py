"""Security-related configuration and validation helpers for pocket-claude."""

from __future__ import annotations

import os
import re
import shutil


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


SKIP_SSL_VERIFY = env_bool("SKIP_SSL_VERIFY", False)
ALLOW_ALL_USERS = env_bool("ALLOW_ALL_USERS", False)
FILE_ALLOW_DIRS = os.getenv("FILE_ALLOW_DIRS", "")
APPROVAL_TOKEN = os.getenv("APPROVAL_TOKEN", "")


def is_safe_session_name(name: str) -> bool:
    """Keep tmux targets simple and predictable."""
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name or ""))


def validate_session_name(name: str) -> str | None:
    if is_safe_session_name(name):
        return None
    return "session 名只能包含字母、数字、点、下划线、短横线，长度 1-64"


def configured_file_allow_dirs(raw: str | None = None):
    """Return absolute allowlisted directories for user-triggered /file."""
    raw = FILE_ALLOW_DIRS if raw is None else raw
    dirs = []
    for item in raw.split(":"):
        item = item.strip()
        if item:
            dirs.append(os.path.realpath(os.path.expanduser(item)))
    return dirs


def is_user_file_allowed(file_path: str, allow_dirs: list[str] | None = None) -> tuple[bool, str]:
    """Restrict user-triggered /file to FILE_ALLOW_DIRS. Auto image push bypasses this."""
    allow_dirs = configured_file_allow_dirs() if allow_dirs is None else allow_dirs
    if not allow_dirs:
        return False, "安全起见，/file 默认关闭。请在 .env 配置 FILE_ALLOW_DIRS 后再使用。"

    real = os.path.realpath(os.path.expanduser(file_path))
    if not os.path.isfile(real):
        return False, f"文件不存在: {real}"

    normalized = [os.path.realpath(os.path.expanduser(d)) for d in allow_dirs]
    for base in normalized:
        try:
            if os.path.commonpath([real, base]) == base:
                return True, real
        except ValueError:
            continue
    return False, "该文件不在 FILE_ALLOW_DIRS 白名单目录内"


def approval_token_ok(parts: list[str], token: str | None = None) -> bool:
    """If APPROVAL_TOKEN is set, /y and /n must be sent as `/y <token>`."""
    token = APPROVAL_TOKEN if token is None else token
    if not token:
        return True
    return len(parts) >= 2 and parts[1] == token


def whitelist_allows_sender(allowed_user_id: str | None, sender_open_id: str | None, allow_all: bool = ALLOW_ALL_USERS) -> bool:
    """Return whether an inbound sender is allowed to control the bridge."""
    if allow_all:
        return True
    if not allowed_user_id:
        return False
    return sender_open_id == allowed_user_id


def doctor_report(app_id: str | None, app_secret: str | None, allowed_user_id: str | None, claude_dir: str, codex_dir: str) -> str:
    """Return a compact local health/security report."""
    from parsers import parser_metadata

    checks = []
    skip_ssl_verify = env_bool("SKIP_SSL_VERIFY", SKIP_SSL_VERIFY)
    allow_all_users = env_bool("ALLOW_ALL_USERS", ALLOW_ALL_USERS)
    approval_token = os.getenv("APPROVAL_TOKEN", APPROVAL_TOKEN)
    file_allow_dirs = os.getenv("FILE_ALLOW_DIRS", FILE_ALLOW_DIRS)

    def mark(ok, label, detail=""):
        checks.append(f"{'✅' if ok else '⚠️'} {label}" + (f": {detail}" if detail else ""))

    allow_dirs = configured_file_allow_dirs(file_allow_dirs)
    mark(bool(app_id and app_secret), "Feishu credentials", "APP_ID/APP_SECRET 已配置" if app_id and app_secret else "缺少 APP_ID 或 APP_SECRET")
    mark(bool(allowed_user_id) or allow_all_users, "User whitelist", "ALLOWED_USER_ID 已配置" if allowed_user_id else "ALLOW_ALL_USERS=true，所有用户可控制")
    mark(not skip_ssl_verify, "SSL verification", "开启" if not skip_ssl_verify else "已关闭，仅建议代理 MITM 调试时使用")
    mark(bool(allow_dirs), "/file allowlist", ", ".join(allow_dirs) if allow_dirs else "未配置，/file 用户命令默认关闭")
    mark(bool(approval_token), "Approval token", "已启用 /y <token>" if approval_token else "未启用，/y /n 无二次口令")
    mark(bool(shutil.which("tmux")), "tmux", shutil.which("tmux") or "未找到")
    mark(bool(shutil.which("claude")), "Claude Code CLI", shutil.which("claude") or "未找到")
    mark(bool(shutil.which("codex")), "Codex CLI", shutil.which("codex") or "未找到")
    mark(os.path.isdir(claude_dir), "Claude log dir", claude_dir)
    mark(os.path.isdir(codex_dir), "Codex log dir", codex_dir)
    parser_versions = ", ".join(f"{item.agent}:{item.format_version}" for item in parser_metadata())
    mark(True, "Parser compatibility", parser_versions)

    return "🩺 Bridge Doctor\n\n" + "\n".join(checks)
