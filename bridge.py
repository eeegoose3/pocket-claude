"""
tmux-bridge: Control Claude Code, Codex, or generic CLI agents from your phone via IM (Feishu/Lark)

Usage: cd ~/Claude_code/tmux-bridge && venv/bin/python bridge.py
Requires: .env with APP_ID, APP_SECRET, ALLOWED_USER_ID (see .env.example)
"""

from app import main


if __name__ == "__main__":
    main()
