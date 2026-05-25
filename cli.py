"""Command line interface for pocket-claude."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from backends import CLAUDE_PROJECTS_DIR, CODEX_SESSIONS_DIR

VERSION = "0.2.0"
DEFAULT_ENV = """# Feishu/Lark App credentials
# Create an app at https://open.feishu.cn/app
APP_ID=cli_xxxxxxxxxxxx
APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Only accept messages from this user (Feishu open_id)
ALLOWED_USER_ID=ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Default CLI backend: claude, codex, or generic
DEFAULT_AGENT=claude

# Command used by generic backend when /start generic is used
GENERIC_CLI_COMMAND=$SHELL

# SECURITY: keep SSL verification on by default.
SKIP_SSL_VERIFY=false

# SECURITY: only set true if every sender should be able to control the bridge.
ALLOW_ALL_USERS=false

# SECURITY: user-triggered /file is disabled by default.
# Set colon-separated allowlisted directories, e.g. ~/Downloads:~/Claude_code
FILE_ALLOW_DIRS=

# Optional second factor for approvals. If set, use /y <token> or /n <token>.
APPROVAL_TOKEN=
"""


def load_env(path: str | None = None) -> None:
    """Load dotenv file before reading env-backed settings."""
    load_dotenv(path or ".env", override=True)


def cmd_run(args: argparse.Namespace) -> int:
    load_env(args.env)
    from app import BridgeRuntime

    BridgeRuntime().run()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    load_env(args.env)
    from security import doctor_report

    app_id = os.getenv("APP_ID")
    app_secret = os.getenv("APP_SECRET")
    allowed_user_id = os.getenv("ALLOWED_USER_ID")
    print(doctor_report(app_id, app_secret, allowed_user_id, CLAUDE_PROJECTS_DIR, CODEX_SESSIONS_DIR))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.env)
    if target.exists() and not args.force:
        print(f"{target} already exists. Use --force to overwrite.", file=sys.stderr)
        return 1

    if args.from_example:
        source = Path(args.from_example)
    else:
        source = Path(".env.example")

    if source.exists():
        content = source.read_text()
    else:
        content = DEFAULT_ENV

    target.write_text(content)
    print(f"Created {target}")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(VERSION)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pocket-claude",
        description="Control Claude Code, Codex, or tmux-based CLI agents from your phone.",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Start the Feishu/tmux bridge")
    run.add_argument("--env", default=".env", help="Path to .env file, default: .env")
    run.set_defaults(func=cmd_run)

    doctor = sub.add_parser("doctor", help="Check configuration and local dependencies")
    doctor.add_argument("--env", default=".env", help="Path to .env file, default: .env")
    doctor.set_defaults(func=cmd_doctor)

    init = sub.add_parser("init", help="Create a starter .env file")
    init.add_argument("--env", default=".env", help="Path to write, default: .env")
    init.add_argument("--force", action="store_true", help="Overwrite existing file")
    init.add_argument("--from-example", help="Template file to copy, default: .env.example if present")
    init.set_defaults(func=cmd_init)

    version = sub.add_parser("version", help="Print version")
    version.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
