# pocket-claude

Control Claude Code, Codex, or any tmux-based CLI agent from your phone — through any IM app you already use.

## Why

I often start multiple CLI agent sessions (Claude Code, Codex, or other terminal agents) on my Mac, then need to step away. But the conversations are stuck in the terminal. Claude Code's official [Remote Control](https://code.claude.com/docs/en/remote-control) lets you continue from the Claude app, and [Channels](https://code.claude.com/docs/en/channels) adds Telegram/Discord/iMessage — but neither solves multi-session management well:

- **Remote Control**: works great for one session, but managing 5+ sessions means switching between them in the Claude app with no IM-style notification flow
- **Channels**: each Claude Code process binds to one bot — there's no routing layer to map different chats to different sessions

pocket-claude takes a different approach: **one bridge process manages all your CLI agent sessions**, with each IM chat mapped to a specific tmux session. Send a message in chat A, it goes to session A. Chat B goes to session B. No ambiguity, no manual switching.

```
Phone (Feishu/Lark today)
        │
        ▼
im_adapter.py  ←→  feishu_adapter.py  ←→  app.py / BridgeRuntime  ←→  commands.py
        │                         │                    │
        │                         ▼                    ▼
        │                    monitor.py          session_runtime.py
        │                         │                    │
        └────────────── notifications/files      tmux.py → CLI agent ×N
                                  │
                                  ▼
                   Claude JSONL / Codex JSONL / screen fallback
```

`bridge.py` is intentionally tiny: it only imports `app.main()` and starts the runtime. The long-lived process state lives in `BridgeRuntime`, while command routing, monitoring, IM I/O, tmux helpers, backend discovery, state persistence, and formatting are split into focused modules.

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

### Runtime architecture

The bridge is organized around a small runtime object plus focused helper modules:

| Layer | Module | Responsibility |
|-------|--------|----------------|
| Entry point | `bridge.py` | Thin executable wrapper |
| Runtime wiring | `app.py` / `BridgeRuntime` | Owns process state, builds contexts, injects the IM adapter, starts Feishu WebSocket and monitor thread |
| IM contract | `im_adapter.py` | Provider-neutral `IMAdapter` / `IMContext` interface used by the bridge core |
| Feishu adapter | `feishu_adapter.py` | Feishu/Lark messages, files, chats, inbound events, reconnect catch-up |
| Command routing | `commands.py` | `/start`, `/sessions`, `/bind`, `/screen`, approvals, text forwarding |
| Monitoring | `monitor.py` | JSONL tailing, screen fallback, permission/image/menu/plan detection |
| Session runtime | `session_runtime.py`, `tmux.py` | backend inference, tmux creation, send-keys, caffeinate |
| Backend logic | `backends.py`, `parsers.py`, `history.py` | Claude/Codex log discovery, JSONL parsing, recent history |
| Safety/state/output | `security.py`, `state.py`, `formatting.py` | security checks, SQLite-backed persisted state, output cleanup |

**Two-layer detection** for maximum reliability:

| Layer | Source | Detects |
|-------|--------|---------|
| Claude JSONL | `~/.claude/projects/**/*.jsonl` | AskUserQuestion, ExitPlanMode, tool_use permissions, system events, turn completion |
| Codex JSONL | `~/.codex/sessions/**/*.jsonl` | user/assistant messages, task completion, escalated command prompts |
| Screen | tmux capture-pane | Fallback for menus, plan prompts, generic CLI output, current input target classification |

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
venv/bin/pip install -e .
```

### Configure

```bash
cp .env.example .env
# Edit .env with your Feishu app credentials

# Or create the starter file through the CLI:
venv/bin/pocket-claude init
```

### Run

The bridge must run in a tmux session (foreground, not background):

```bash
# Create a dedicated tmux session for the bridge
tmux new-session -s bridge
cd ~/path/to/pocket-claude
venv/bin/pocket-claude run
```

`venv/bin/python bridge.py` still works as a compatibility entry point.
Use `venv/bin/pocket-claude doctor` for a local preflight check before starting the bridge.


## Security defaults

This bridge can control your local terminal, so the defaults are intentionally conservative:

- `ALLOWED_USER_ID` is required by default. Without it, the bridge refuses to handle messages unless `ALLOW_ALL_USERS=true` is explicitly set.
- SSL verification stays enabled by default. `SKIP_SSL_VERIFY=true` is only for local proxy/MITM debugging.
- User-triggered `/file <path>` is disabled by default. Set `FILE_ALLOW_DIRS` to a colon-separated allowlist before using it.
- Optional approval second factor: set `APPROVAL_TOKEN`, then approve with `/y <token>` or reject with `/n <token>`.
- tmux session names are restricted to letters, numbers, `.`, `_`, `-` and max 64 chars.

Run `/doctor` in Feishu to check the current configuration and local CLI dependencies.
From the terminal, run `venv/bin/pocket-claude doctor` for the same local check.

## Commands

### Global commands (any chat)

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/doctor` | Check security config and local CLI dependencies |
| `/status` | Show current Feishu chat → tmux binding, tmux online/missing state, and likely input target |
| `/sessions` | List running tmux sessions with numbered entries and likely input target (`/list` is an alias) |
| `/bind <name-or-number> [claude|codex|generic]` | Bind the current Feishu chat to a running tmux session |
| `/new <name> [claude|codex|generic]` | Create a Feishu chat for an existing tmux session |
| `/start [claude|codex] <name> <dir>` | Create a new tmux session + start selected CLI + create Feishu chat |
| `/resume [claude|codex] <name> <session-id>` | Advanced compatibility command: create a new tmux session and run the agent's own resume command |
| `/caffeinate` | Toggle macOS sleep prevention |

`/start` and `/resume` do not inject commands into an existing tmux session. If the target tmux session already exists, bind to it with `/bind` or choose a new session name.

### Backend selection examples

```bash
# Start Claude Code (backward-compatible default if DEFAULT_AGENT=claude)
/start claude marketing ~/Claude_code/marketing

# Start Codex
/start codex marketing ~/Claude_code/marketing

# List and bind already-running tmux sessions
/sessions
/bind 1
/bind marketing generic

# Advanced: create a new tmux session and run Codex resume inside it
/resume codex marketing-restored 019e5e21-b1a3-75c2-8521-5391b4ff644b
```

Set `DEFAULT_AGENT=codex` in `.env` if you primarily use Codex.

### Session commands (in a bound chat)

| Command | Description |
|---------|-------------|
| `/screen` | Capture current tmux screen (last 50 lines) and show likely input target |
| `/file <path>` | Send a local file to Feishu; requires `FILE_ALLOW_DIRS` |
| `/y [token]` | Approve (send `y` to the CLI); token required if `APPROVAL_TOKEN` is set |
| `/n [token]` | Reject (send `n` to the CLI); token required if `APPROVAL_TOKEN` is set |
| `/cancel` | Send Ctrl+C |
| `/remote` | Manually enter remote mode |
| `/local` | Manually exit remote mode |
| `/unbind` | Unbind this Feishu chat from its tmux session |
| *(any text)* | Type into the bound tmux session; if the screen looks like shell and the text is natural language, the bridge asks you to start Codex/Claude first |

### tmux session model

The stable routing model is:

```
Feishu chat → tmux session → whatever CLI is currently running there
```

Feishu chats bind to tmux sessions. Codex/Claude JSONL IDs are only recent agent history used by monitoring; they are not the main identity of a remote session. If a bound tmux session no longer exists, the bridge keeps the binding record and explains how to re-bind or recreate instead of silently unbinding.

`/screen`, `/status`, and `/sessions` classify the visible prompt to show the likely input target: Codex, Claude Code, Shell, menu, or unknown. This is based on the current tmux screen rather than process-name guesses.

### Selection menus

When Claude Code presents a selection menu (AskUserQuestion), the options are pushed to Feishu with numbers. Reply with a number to select, or type free text for "Other".

### Permission confirmations

When Claude Code or Codex needs permission to run a command or edit a file, you'll get a notification with `/y` to approve or `/n` to reject.

## Files

| File | Description |
|------|-------------|
| `bridge.py` | Thin executable entry point |
| `cli.py` | `pocket-claude` CLI entry point (`run`, `doctor`, `init`, `version`) |
| `pyproject.toml` | Editable-install metadata and console script definition |
| `.env.example` | Starter environment template |
| `app.py` | BridgeRuntime application state, context wiring, and lifecycle startup |
| `im_adapter.py` | Provider-neutral IM adapter protocol and runtime context |
| `feishu_adapter.py` | Feishu/Lark implementation of the IM adapter contract |
| `commands.py` | Command routing for tmux binding/start/screen/status, approvals, and text forwarding |
| `monitor.py` | Background JSONL/screen monitor, permission/image/menu detection |
| `screen_classifier.py` | Visible prompt classifier for Codex/Claude/Shell/menu input target |
| `remote_mode.py` | Remote/local mode state and history-context notifications |
| `history.py` | Recent conversation history loader from agent JSONL logs |
| `session_runtime.py` | tmux session runtime helpers, backend inference, and caffeinate |
| `backends.py` | Claude/Codex/generic backend helpers: commands, log discovery, cwd lookup |
| `security.py` | Security configuration and validation helpers |
| `tmux.py` | tmux command helpers |
| `state.py` | SQLite-backed state store with legacy JSON migration |
| `formatting.py` | Output cleanup and Markdown/table formatting helpers |
| `parsers.py` | Versioned `ClaudeJsonlParser` / `CodexJsonlParser` / `ScreenParser` compatibility layer |
| `tests/test_parsers.py` | Minimal parser compatibility tests |
| `tests/test_parser_fixtures.py` | Claude/Codex JSONL fixture contract tests |
| `tests/test_cli.py` | CLI command tests |
| `tests/fixtures/` | Small anonymized JSONL samples used by parser tests |
| `tests/test_app.py` | Minimal BridgeRuntime wiring tests |
| `tests/test_backends.py` | Minimal backend helper tests |
| `tests/test_commands.py` | Minimal command routing tests |
| `tests/test_feishu_adapter.py` | Minimal Feishu adapter tests |
| `tests/test_formatting.py` | Minimal output formatting tests |
| `tests/test_history.py` | Minimal conversation history tests |
| `tests/test_monitor.py` | Minimal monitor helper tests |
| `tests/test_remote_mode.py` | Minimal remote-mode tests |
| `tests/test_screen_classifier.py` | Minimal screen input target classifier tests |
| `tests/test_security.py` | Minimal security helper tests |
| `tests/test_session_runtime.py` | Minimal session runtime tests |
| `tests/test_tmux.py` | Minimal tmux helper tests |
| `tests/test_state.py` | Minimal state persistence tests |
| `TESTING.md` | Automated and manual smoke-test notes |
| `.env` | Feishu credentials (not committed) |
| `bridge_state.db` | SQLite state store: chat bindings, backend bindings, JSONL IDs, runtime mode metadata (auto-generated, not committed) |
| `bindings.json` | Legacy chat ↔ session mappings; migrated into SQLite on first load if present |
| `jsonl_ids.json` | Legacy session ↔ agent JSONL/session-id mappings; migrated into SQLite on first load if present |
| `session_backends.json` | Legacy session ↔ backend mappings; migrated into SQLite on first load if present |

## Adapting to other IM platforms

The bridge core now talks to a provider-neutral `IMAdapter` interface in `im_adapter.py`. Feishu/Lark is the first concrete implementation in `feishu_adapter.py`; Feishu WebSocket startup still lives in `BridgeRuntime.run()` because it is provider-specific process wiring.

To add another IM platform, keep the core modules unchanged and implement an adapter with equivalent responsibilities:

- outbound text/card messages
- file/image upload
- chat creation or chat binding
- inbound message parsing and whitelist enforcement
- reconnect recovery or missed-message catch-up, if the platform supports it

The platform-agnostic core is already separated: command routing (`commands.py`), monitoring (`monitor.py`), remote mode (`remote_mode.py`), tmux/session runtime (`session_runtime.py`, `tmux.py`), backend parsing (`backends.py`, `parsers.py`, `history.py`), security (`security.py`), and persistence (`state.py`). `BridgeRuntime` accepts an injected adapter, so adapter behavior can be tested without a live Feishu client.

## Contributing

This project is built and maintained by one person (with a lot of help from Claude). Contributions are welcome:

- **Bug reports** — if something breaks, open an issue with your terminal output and steps to reproduce
- **Bug fixes** — PRs for fixes are always appreciated, especially edge cases I haven't hit yet
- **New IM adapters** — want to use this with Telegram, Slack, Discord, or WeChat? Implement the `IMAdapter` contract and keep bridge core modules unchanged
- **Ideas and feedback** — open an issue or start a discussion

## Development

See `TESTING.md` for automated checks and manual smoke-test notes.


```bash
python3 -m py_compile bridge.py app.py cli.py backends.py parsers.py security.py tmux.py state.py formatting.py commands.py monitor.py screen_classifier.py im_adapter.py feishu_adapter.py remote_mode.py history.py session_runtime.py
python3 -m unittest discover -v
venv/bin/pocket-claude version
venv/bin/pocket-claude doctor
```

## Known limitations

- WebSocket disconnects are a known behavior of the Feishu Python SDK; reconnect is optimized to < 1 second, with automatic message recovery
- JSONL file matching uses screen content cross-verification when multiple sessions share the same project directory
- Codex support is based on the current `~/.codex/sessions/**/*.jsonl` format and `codex resume <session-id>` command
- Generic CLI support has no structured log; it uses tmux screen-change forwarding
- tmux server must be started from a GUI terminal (Terminal.app) for Keychain access to work

## License

MIT
