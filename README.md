# pocket-claude

Control Claude Code, Codex, or any tmux-based CLI agent from your phone — through any IM app you already use.

## Why

I often start multiple CLI agent sessions (Claude Code, Codex, or other terminal agents) on my Mac, then need to step away. But the conversations are stuck in the terminal. Claude Code's official [Remote Control](https://code.claude.com/docs/en/remote-control) lets you continue from the Claude app, and [Channels](https://code.claude.com/docs/en/channels) adds Telegram/Discord/iMessage — but neither solves multi-session management well:

- **Remote Control**: works great for one session, but managing 5+ sessions means switching between them in the Claude app with no IM-style notification flow
- **Channels**: each Claude Code process binds to one bot — there's no routing layer to map different chats to different sessions

pocket-claude takes a different approach: **one bridge process manages all your CLI agent sessions**, with each IM chat mapped to a specific tmux session. Send a message in chat A, it goes to session A. Chat B goes to session B. No ambiguity, no manual switching.

```
Phone (IM app) ←→ WebSocket ←→ bridge.py ←→ tmux send-keys ←→ CLI agent ×N
                                    ↑
                           backend monitor
              (Claude JSONL / Codex JSONL / screen fallback)
```

## How it compares

|  | pocket-claude | Remote Control | Channels |
|---|---|---|---|
| Multi-session routing | One chat per session, automatic | Switch manually in Claude app | One bot per session, no routing |
| Zero config on CLI side | Works with any running tmux session | Need `/remote-control` per session | Need `--channels` flag at startup |
| IM platform | Feishu (more coming) | Claude app only | Telegram, Discord, iMessage |
| Interactive UI forwarding | Selection menus, plan approvals, permission prompts | Full native UI | Text only |
| Works offline → reconnect | Auto message recovery | Session times out after ~10 min | No recovery |

## What it does

- **Multi-session hub**: one bridge manages Claude Code, Codex, or generic CLI sessions, each mapped to its own IM chat
- Send messages to a CLI agent from your phone, get replies pushed back in real-time
- Detect and forward interactive UIs where structured logs exist: selection menus, plan approvals, permission confirmations
- Auto-push images that Claude Code generates (Write tool + Playwright screenshots)
- Seamlessly switch between phone and computer — local keyboard input auto-deactivates remote mode

## How it works

**Two-layer detection** for maximum reliability:

| Layer | Source | Detects |
|-------|--------|---------|
| Claude JSONL | `~/.claude/projects/**/*.jsonl` | AskUserQuestion, ExitPlanMode, tool_use permissions, system events, turn completion |
| Codex JSONL | `~/.codex/sessions/**/*.jsonl` | user/assistant messages, task completion, escalated command prompts |
| Screen | tmux capture-pane | Fallback for menus, plan prompts, generic CLI output |

**Remote mode state machine:**
- **Local mode** (default): bridge monitors silently, no push notifications
- **Remote mode**: activated when you send a message from Feishu; CLI output is pushed to your phone
- Auto-exits when local keyboard input is detected in Claude/Codex JSONL

## Setup

### Prerequisites

- macOS with tmux installed
- Python 3.9+
- Claude Code and/or Codex installed if you want structured backend support
- Any other CLI can still be controlled through generic tmux screen fallback
- A [Feishu app](https://open.feishu.cn/app) with these permissions:
  - `im:message` — send/receive messages
  - `im:chat` — create group chats
  - `im:resource` — upload files/images
  - Enable **Bot** capability and **WebSocket** event subscription

### Install

```bash
git clone https://github.com/eeegoose3/pocket-claude.git
cd pocket-claude
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env with your Feishu app credentials
```

### Run

The bridge must run in a tmux session (foreground, not background):

```bash
# Create a dedicated tmux session for the bridge
tmux new-session -s bridge
cd ~/path/to/tmux-bridge
venv/bin/python bridge.py
```


## Security defaults

This bridge can control your local terminal, so the defaults are intentionally conservative:

- `ALLOWED_USER_ID` is required by default. Without it, the bridge refuses to handle messages unless `ALLOW_ALL_USERS=true` is explicitly set.
- SSL verification stays enabled by default. `SKIP_SSL_VERIFY=true` is only for local proxy/MITM debugging.
- User-triggered `/file <path>` is disabled by default. Set `FILE_ALLOW_DIRS` to a colon-separated allowlist before using it.
- Optional approval second factor: set `APPROVAL_TOKEN`, then approve with `/y <token>` or reject with `/n <token>`.
- tmux session names are restricted to letters, numbers, `.`, `_`, `-` and max 64 chars.

Run `/doctor` in Feishu to check the current configuration and local CLI dependencies.

## Commands

### Global commands (any chat)

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/doctor` | Check security config and local CLI dependencies |
| `/list` | List all tmux sessions |
| `/status` | Global status overview (backend, mode, log binding, WebSocket, caffeinate) |
| `/new <name> [claude|codex|generic]` | Create a Feishu chat for an existing tmux session |
| `/start [claude|codex] <name> <dir>` | Create tmux session + start selected CLI + create Feishu chat |
| `/resume [claude|codex] <name> <session-id>` | Resume a Claude/Codex conversation in a new tmux session |
| `/caffeinate` | Toggle macOS sleep prevention |


### Backend selection examples

```bash
# Start Claude Code (backward-compatible default if DEFAULT_AGENT=claude)
/start claude marketing ~/Claude_code/marketing

# Start Codex
/start codex marketing ~/Claude_code/marketing

# Resume a Codex session
/resume codex marketing 019e5e21-b1a3-75c2-8521-5391b4ff644b

# Bind an already-running tmux session and force backend
/new marketing codex
/bind marketing generic
```

Set `DEFAULT_AGENT=codex` in `.env` if you primarily use Codex.

### Session commands (in a bound chat)

| Command | Description |
|---------|-------------|
| `/screen` | Capture current screen (last 50 lines) |
| `/file <path>` | Send a local file to Feishu; requires `FILE_ALLOW_DIRS` |
| `/y [token]` | Approve (send `y` to the CLI); token required if `APPROVAL_TOKEN` is set |
| `/n [token]` | Reject (send `n` to the CLI); token required if `APPROVAL_TOKEN` is set |
| `/cancel` | Send Ctrl+C |
| `/remote` | Manually enter remote mode |
| `/local` | Manually exit remote mode |
| `/unbind` | Unbind this chat from its session |
| *(any text)* | Send directly to the bound CLI session |

### Selection menus

When Claude Code presents a selection menu (AskUserQuestion), the options are pushed to Feishu with numbers. Reply with a number to select, or type free text for "Other".

### Permission confirmations

When Claude Code or Codex needs permission to run a command or edit a file, you'll get a notification with `/y` to approve or `/n` to reject.

## Files

| File | Description |
|------|-------------|
| `bridge.py` | Runtime bridge: Feishu wiring, lifecycle, monitors |
| `commands.py` | Command routing for `/start`, `/resume`, `/screen`, approvals, and text forwarding |
| `monitor.py` | Background JSONL/screen monitor, permission/image/menu detection |
| `backends.py` | Claude/Codex/generic backend helpers: commands, log discovery, cwd lookup |
| `security.py` | Security configuration and validation helpers |
| `tmux.py` | tmux command helpers |
| `state.py` | Persistent runtime state helpers |
| `formatting.py` | Output cleanup and Markdown/table formatting helpers |
| `parsers.py` | Pure Claude/Codex JSONL parser functions |
| `tests/test_parsers.py` | Minimal parser compatibility tests |
| `tests/test_backends.py` | Minimal backend helper tests |
| `tests/test_commands.py` | Minimal command routing tests |
| `tests/test_formatting.py` | Minimal output formatting tests |
| `tests/test_monitor.py` | Minimal monitor helper tests |
| `tests/test_security.py` | Minimal security helper tests |
| `tests/test_tmux.py` | Minimal tmux helper tests |
| `tests/test_state.py` | Minimal state persistence tests |
| `TESTING.md` | Automated and manual smoke-test notes |
| `.env` | Feishu credentials (not committed) |
| `bindings.json` | Chat ↔ session mappings (auto-generated, not committed) |
| `jsonl_ids.json` | Session ↔ agent JSONL/session-id mappings (auto-generated, not committed) |
| `session_backends.json` | Session ↔ backend mappings (`claude`, `codex`, `generic`) |

## Adapting to other IM platforms

The codebase separates **IM Layer** (Feishu-specific) from **Core Logic** (platform-agnostic). Functions marked with `[IM-LAYER]` in comments are the ones to replace:

- `send_feishu_msg()` — outbound messaging
- `send_feishu_file()` — file/image upload
- `create_feishu_chat()` — chat creation
- `on_message()` — inbound message handling
- `catchup_missed_messages()` — reconnect recovery
- `main()` → client initialization section

Core logic (backend log parsing, tmux operations, command routing, remote mode) works with any IM backend.

## Contributing

This project is built and maintained by one person (with a lot of help from Claude). Contributions are welcome:

- **Bug reports** — if something breaks, open an issue with your terminal output and steps to reproduce
- **Bug fixes** — PRs for fixes are always appreciated, especially edge cases I haven't hit yet
- **New IM adapters** — want to use this with Telegram, Slack, Discord, or WeChat? The IM layer is separated and marked with `[IM-LAYER]` in the code
- **Ideas and feedback** — open an issue or start a discussion

## Development

See `TESTING.md` for automated checks and manual smoke-test notes.


```bash
python3 -m py_compile bridge.py backends.py parsers.py security.py tmux.py state.py formatting.py commands.py monitor.py
python3 -m unittest discover -v
```

## Known limitations

- WebSocket disconnects are a known behavior of the Feishu Python SDK; reconnect is optimized to < 1 second, with automatic message recovery
- JSONL file matching uses screen content cross-verification when multiple sessions share the same project directory
- Codex support is based on the current `~/.codex/sessions/**/*.jsonl` format and `codex resume <session-id>` command
- Generic CLI support has no structured log; it uses tmux screen-change forwarding
- tmux server must be started from a GUI terminal (Terminal.app) for Keychain access to work

## License

MIT
