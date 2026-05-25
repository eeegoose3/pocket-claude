# Testing

## Automated checks

```bash
python3 -m py_compile bridge.py app.py cli.py backends.py parsers.py security.py tmux.py state.py formatting.py commands.py monitor.py screen_classifier.py im_adapter.py feishu_adapter.py remote_mode.py history.py session_runtime.py
python3 -m unittest discover -v
git diff --check
```

## Local CLI checks

After installing in editable mode:

```bash
venv/bin/pip install -e .
venv/bin/pocket-claude version
venv/bin/pocket-claude doctor --env .env
venv/bin/pocket-claude init --env /tmp/pocket-claude.env --force
```

Do not paste real `.env` values into issues, PRs, or logs.

## State persistence

Runtime state is stored in `bridge_state.db` (SQLite). Existing `bindings.json`, `jsonl_ids.json`, and `session_backends.json` files are treated as legacy migration inputs on first load.

State tests cover:

- missing-state bootstrap
- save/load through SQLite
- legacy JSON → SQLite migration
- SQLite winning over stale legacy JSON after migration
- persisted remote/local mode metadata

## Parser fixture contract

`parsers.py` exposes versioned parser classes:

- `ClaudeJsonlParser`
- `CodexJsonlParser`
- `ScreenParser`

`/doctor` and `pocket-claude doctor` show the active parser compatibility versions so field reports can include the parser contract in use.

`tests/fixtures/claude_sample.jsonl` and `tests/fixtures/codex_sample.jsonl` are small anonymized samples that lock the minimum JSONL compatibility contract:

- user text extraction
- assistant text extraction
- permission/tool-pending detection
- tool-result matching
- turn-complete/system-event detection

## IM adapter boundary

The bridge core depends on `im_adapter.IMAdapter` / `IMContext`; Feishu/Lark is implemented in `feishu_adapter.FeishuAdapter`.

Tests cover:

- inbound Feishu text parsing and whitelist filtering
- safe no-client send behavior
- BridgeRuntime adapter injection, so core runtime behavior can be tested without a live Feishu SDK client

## tmux session UX checks

The user-facing routing model is `Feishu chat → tmux session → current CLI/shell`. Automated tests cover:

- screen prompt classification for Codex and shell
- `/sessions`/`/bind <number>` style tmux binding
- missing tmux sessions keeping their Feishu binding record instead of auto-unbinding
- shell prompt protection: natural language is not sent to shell, while commands such as `codex` are sent
- `/start` refusing to inject startup commands into an existing tmux session

## Manual Feishu + tmux smoke test

Date: 2026-05-25

Latest manual smoke test was run after the `BridgeRuntime` refactor on merged `main`.

Environment:
- macOS
- Feishu bot via WebSocket
- tmux installed at `/opt/homebrew/bin/tmux`
- Codex CLI installed at `/opt/homebrew/bin/codex`
- `SKIP_SSL_VERIFY=true` required locally because the network proxy uses a self-signed certificate chain

Verified:
- `/doctor` returns configuration and dependency status
- `/start codex test /Users/chouduck/funny` and `/start codex rt-test /Users/chouduck/funny` create tmux sessions and Feishu chats
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
