# Testing

## Automated checks

```bash
python3 -m py_compile bridge.py backends.py parsers.py security.py tmux.py state.py formatting.py commands.py monitor.py feishu_adapter.py
python3 -m unittest discover -v
git diff --check
```

## Manual Feishu + tmux smoke test

Date: 2026-05-25

Environment:
- macOS
- Feishu bot via WebSocket
- tmux installed at `/opt/homebrew/bin/tmux`
- Codex CLI installed at `/opt/homebrew/bin/codex`
- `SKIP_SSL_VERIFY=true` required locally because the network proxy uses a self-signed certificate chain

Verified:
- `/doctor` returns configuration and dependency status
- `/start codex test /Users/chouduck/funny` creates a tmux session and Feishu chat
- Sending `你好，回复OK` from Feishu reaches Codex
- Codex response is pushed back to Feishu
- Turn-complete notification is pushed back to Feishu
- `/screen` returns the tmux pane contents
- `/file /etc/passwd` is rejected when `FILE_ALLOW_DIRS` is unset
- `/cancel` sends Ctrl+C to the bound tmux session

Not yet verified:
- `/resume codex <name> <session-id>` end-to-end from Feishu
- Approval flows with `/y` and `/n`
- Approval token flow with `APPROVAL_TOKEN`
- Claude Code backend after refactor
