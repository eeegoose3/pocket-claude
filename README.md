# tmux-bridge

Control Claude Code remotely from your phone via Feishu (Lark).

## Why

I often start a Claude Code session on my Mac, then need to step away — grab coffee, commute, or just move to the couch. But the conversation is stuck in the terminal. I wanted a way to keep talking to Claude Code from my phone without interrupting what's already running.

So I built this bridge: it sits between Feishu (the IM app I use daily) and tmux, forwarding messages both ways. No need to start a new session — you pick up exactly where you left off.

```
Phone (Feishu) ←→ WebSocket ←→ bridge.py ←→ tmux send-keys ←→ Claude Code
                                    ↑
                              JSONL monitor
                        (reads Claude's conversation logs)
```

## What it does

- Send messages to Claude Code from your phone, get replies pushed back in real-time
- Each tmux session gets its own Feishu group chat — manage multiple Claude instances independently
- Detect and forward interactive UIs: selection menus, plan approvals, permission confirmations
- Auto-push images that Claude generates (Write tool + Playwright screenshots)
- Seamlessly switch between phone and computer — local keyboard input auto-deactivates remote mode

## How it works

**Two-layer detection** for maximum reliability:

| Layer | Source | Detects |
|-------|--------|---------|
| JSONL | Claude's conversation log files | AskUserQuestion, ExitPlanMode, tool_use permissions, system events, turn completion |
| Screen | tmux capture-pane | Fallback for menus, plan prompts |

**Remote mode state machine:**
- **Local mode** (default): bridge monitors silently, no push notifications
- **Remote mode**: activated when you send a message from Feishu; all Claude output is pushed to your phone
- Auto-exits when local keyboard input is detected in JSONL

## Setup

### Prerequisites

- macOS with tmux installed
- Python 3.9+
- Claude Code running in tmux sessions
- A [Feishu app](https://open.feishu.cn/app) with these permissions:
  - `im:message` — send/receive messages
  - `im:chat` — create group chats
  - `im:resource` — upload files/images
  - Enable **Bot** capability and **WebSocket** event subscription

### Install

```bash
git clone https://github.com/eeegoose3/tmux-bridge.git
cd tmux-bridge
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

## Commands

### Global commands (any chat)

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/list` | List all tmux sessions |
| `/status` | Global status overview (mode, JSONL binding, WebSocket, caffeinate) |
| `/new <name>` | Create a Feishu chat for an existing tmux session |
| `/start <name> <dir>` | Create tmux session + start Claude Code + create Feishu chat |
| `/resume <name> <session-id>` | Resume a Claude Code conversation in a new tmux session |
| `/caffeinate` | Toggle macOS sleep prevention |

### Session commands (in a bound chat)

| Command | Description |
|---------|-------------|
| `/screen` | Capture current screen (last 50 lines) |
| `/file <path>` | Send a local file to Feishu (images display inline) |
| `/y` | Approve (send `y` to Claude) |
| `/n` | Reject (send `n` to Claude) |
| `/cancel` | Send Ctrl+C |
| `/remote` | Manually enter remote mode |
| `/local` | Manually exit remote mode |
| `/unbind` | Unbind this chat from its session |
| *(any text)* | Send directly to Claude Code |

### Selection menus

When Claude presents a selection menu (AskUserQuestion), the options are pushed to Feishu with numbers. Reply with a number to select, or type free text for "Other".

### Permission confirmations

When Claude needs permission to run a command or edit a file, you'll get a notification with `/y` to approve or `/n` to reject.

## Files

| File | Description |
|------|-------------|
| `bridge.py` | All logic in one file (~1950 lines) |
| `.env` | Feishu credentials (not committed) |
| `bindings.json` | Chat ↔ session mappings (auto-generated, not committed) |
| `jsonl_ids.json` | Session ↔ JSONL file mappings (auto-generated, not committed) |

## Adapting to other IM platforms

The codebase separates **IM Layer** (Feishu-specific) from **Core Logic** (platform-agnostic). Functions marked with `[IM-LAYER]` in comments are the ones to replace:

- `send_feishu_msg()` — outbound messaging
- `send_feishu_file()` — file/image upload
- `create_feishu_chat()` — chat creation
- `on_message()` — inbound message handling
- `catchup_missed_messages()` — reconnect recovery
- `main()` → client initialization section

Core logic (JSONL parsing, tmux operations, command routing, remote mode) works with any IM backend.

## Contributing

This project is built and maintained by one person (with a lot of help from Claude). Contributions are welcome:

- **Bug reports** — if something breaks, open an issue with your terminal output and steps to reproduce
- **Bug fixes** — PRs for fixes are always appreciated, especially edge cases I haven't hit yet
- **New IM adapters** — want to use this with Telegram, Slack, Discord, or WeChat? The IM layer is separated and marked with `[IM-LAYER]` in the code
- **Ideas and feedback** — open an issue or start a discussion

## Known limitations

- WebSocket disconnects are a known behavior of the Feishu Python SDK; reconnect is optimized to < 1 second, with automatic message recovery
- JSONL file matching uses screen content cross-verification when multiple sessions share the same project directory
- tmux server must be started from a GUI terminal (Terminal.app) for Keychain access to work

## License

MIT
