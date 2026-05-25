"""
tmux-bridge: Control Claude Code, Codex, or generic CLI agents from your phone via IM (Feishu/Lark)

Architecture:
  Phone (Feishu) ←→ [WebSocket] ←→ bridge.py ←→ [tmux send-keys] ←→ CLI agent
                                        ↑
                         backend monitor (Claude JSONL / Codex JSONL / screen)

Two-layer detection:
  - JSONL layer: parses Claude Code and Codex conversation logs for structured events
    (messages, tool/permission events, system events) — faster and richer
  - Screen layer: captures tmux pane content as fallback/supplement

Remote mode state machine:
  Local (default) → Remote: triggered by sending a message from IM
  Remote → Local: triggered by detecting local keyboard input in Claude/Codex JSONL

IM Layer (Feishu-specific, replace these to adapt to other platforms):
  - send_feishu_msg()          — Send text/card message to IM
  - send_feishu_file()         — Upload and send file/image to IM
  - create_feishu_chat()       — Create a new group chat
  - on_message()               — Handle incoming IM messages (WebSocket event)
  - catchup_missed_messages()  — Pull missed messages via REST API after reconnect
  - main() → Lark client init  — WebSocket client setup and lifecycle hooks

Core Logic (platform-agnostic):
  - tmux operations            — send_keys, capture_pane, etc.
  - Backend parsing            — Claude/Codex JSONL + generic screen fallback
  - jsonl_monitor()            — Background thread monitoring logs/screens
  - handle_command()           — Command routing (/help, /screen, /y, /n, etc.)
  - Remote mode management     — enter/exit_remote_mode, ensure_remote_mode

Usage: cd ~/Claude_code/tmux-bridge && venv/bin/python bridge.py
Requires: .env with APP_ID, APP_SECRET, ALLOWED_USER_ID (see .env.example)
"""

from __future__ import annotations

import atexit
import logging
import os
import ssl
import threading
import time

from dotenv import load_dotenv

load_dotenv()

from backends import (
    CLAUDE_PROJECTS_DIR,
    CODEX_SESSIONS_DIR,
)
from commands import CommandContext, handle_command as route_command
from feishu_adapter import (
    FeishuContext,
    catchup_missed_messages as feishu_catchup_missed_messages,
    create_chat as feishu_create_chat,
    on_message as feishu_on_message,
    send_file as feishu_send_file,
    send_message as feishu_send_message,
)
from monitor import MonitorContext, jsonl_monitor as run_jsonl_monitor, menu_notified, menu_state
from remote_mode import (
    RemoteModeContext,
    remote_mode,
    enter_remote_mode as remote_enter_remote_mode,
    exit_remote_mode as remote_exit_remote_mode,
    ensure_remote_mode as remote_ensure_remote_mode,
)
from state import BridgeState, load_state, save_state
from security import (
    ALLOW_ALL_USERS,
    SKIP_SSL_VERIFY,
)
from history import load_recent_history
from session_runtime import (
    SessionRuntimeContext,
    backend_display as runtime_backend_display,
    create_tmux_and_run as runtime_create_tmux_and_run,
    get_backend as runtime_get_backend,
    is_caffeinate_running,
    normalize_agent as runtime_normalize_agent,
    send_keys as runtime_send_keys,
    start_caffeinate,
    stop_caffeinate,
)

# SSL 校验默认开启。只有在用户明确配置 SKIP_SSL_VERIFY=true 时，才为代理 MITM 场景跳过校验。
if SKIP_SSL_VERIFY:
    ssl._create_default_https_context = ssl._create_unverified_context

import requests as _requests
import websockets as _websockets

if SKIP_SSL_VERIFY:
    _orig_requests_post = _requests.post
    def _patched_post(*args, **kwargs):
        kwargs.setdefault("verify", False)
        return _orig_requests_post(*args, **kwargs)
    _requests.post = _patched_post

_orig_ws_connect = _websockets.connect
def _patched_ws_connect(*args, **kwargs):
    if SKIP_SSL_VERIFY and "ssl" not in kwargs:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl"] = ctx
    # 禁用 websockets 库自带的协议层 ping（默认 20 秒），
    # 只依赖 Lark SDK 的应用层心跳（90 秒），和 Node.js SDK 行为一致
    kwargs.setdefault("ping_interval", None)
    kwargs.setdefault("ping_timeout", None)
    return _orig_ws_connect(*args, **kwargs)
_websockets.connect = _patched_ws_connect

# ── Feishu/Lark SDK ──────────────────────────────────────
# To adapt to another IM platform (Slack, Telegram, Discord, etc.),
# replace this import block and the IM Layer functions listed in the module docstring.
import lark_oapi as lark

# ── 配置 ──────────────────────────────────────────────

# Feishu app credentials (create at https://open.feishu.cn/app)
APP_ID = os.getenv("APP_ID")           # Feishu app ID
APP_SECRET = os.getenv("APP_SECRET")   # Feishu app secret
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")  # Only accept messages from this user (open_id)

POLL_INTERVAL = 2          # 对话日志/屏幕轮询间隔（秒）
CAPTURE_LINES = 50         # capture-pane 行数（/screen 用）
MAX_MSG_LEN = 4000         # 飞书单条消息最大字符数
DEFAULT_AGENT = os.getenv("DEFAULT_AGENT", "claude").lower()  # claude / codex / generic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bridge")

# ── 全局状态 ────────────────────────────────────────────

chat_session_map = {}       # {chat_id: session_name} 对话↔session 绑定
session_jsonl_id = {}       # {session_name: agent_session_id} 精确锁定 JSONL 文件
session_backend = {}        # {session_name: "claude"|"codex"|"generic"} 每个 tmux session 使用的 CLI backend
session_start_time = {}     # {session_name: timestamp} /start 启动时间，用于识别新日志
lark_client = None          # 飞书 API 客户端
_reply_chat_id = None       # 当前命令的回复目标 chat_id（临时）
seen_message_ids = set()    # 飞书消息去重（防止重复事件）
last_disconnect_time = 0    # WebSocket 上次断连时间戳
last_connect_time = 0       # WebSocket 上次连接时间戳
bridge_sent_time = {}       # {session_name: float} bridge 最近一次向 CLI 发送的时间
BRIDGE_SENT_WINDOW = 15     # 秒，此窗口内的日志 user 消息视为 bridge 发送


# ── session runtime helpers ───────────────────────────────────────────

def build_session_runtime_context() -> SessionRuntimeContext:
    return SessionRuntimeContext(
        default_agent=DEFAULT_AGENT,
        session_backend=session_backend,
        bridge_sent_time=bridge_sent_time,
    )


def normalize_agent(agent: str | None) -> str:
    return runtime_normalize_agent(agent, DEFAULT_AGENT)


def get_backend(session_name: str | None) -> str:
    return runtime_get_backend(session_name, build_session_runtime_context())


def backend_display(session_name: str | None) -> str:
    return runtime_backend_display(session_name, build_session_runtime_context())


def send_keys(session: str, text: str):
    runtime_send_keys(session, text, build_session_runtime_context())


def create_tmux_and_run(session_name, command):
    return runtime_create_tmux_and_run(session_name, command)


# ── 绑定持久化 ────────────────────────────────────────────

def load_bindings():
    global chat_session_map, session_jsonl_id, session_backend
    loaded = load_state()
    chat_session_map = loaded.chat_session_map
    session_jsonl_id = loaded.session_jsonl_id
    session_backend = {k: normalize_agent(v) for k, v in loaded.session_backend.items()}
    log.info(f"已加载 {len(chat_session_map)} 个绑定")
    log.info(f"已加载 {len(session_jsonl_id)} 个 agent session ID")
    log.info(f"已加载 {len(session_backend)} 个 backend 绑定")


def save_bindings():
    save_state(BridgeState(
        chat_session_map=chat_session_map,
        session_jsonl_id=session_jsonl_id,
        session_backend=session_backend,
    ))


# ── Feishu IM Adapter wrappers ──────────────────────────────

def reset_disconnect_time():
    global last_disconnect_time
    last_disconnect_time = 0


def build_feishu_context() -> FeishuContext:
    return FeishuContext(
        lark_client=lark_client,
        default_chat_id=_reply_chat_id,
        max_msg_len=MAX_MSG_LEN,
        allowed_user_id=ALLOWED_USER_ID,
        allow_all_users=ALLOW_ALL_USERS,
        seen_message_ids=seen_message_ids,
        chat_session_map=chat_session_map,
        remote_mode=remote_mode,
        last_disconnect_time=last_disconnect_time,
        last_connect_time=last_connect_time,
        handle_command=handle_command,
        reset_disconnect_time=reset_disconnect_time,
    )


def send_feishu_msg(text, target_chat_id=None, use_card=None):
    feishu_send_message(text, build_feishu_context(), target_chat_id=target_chat_id, use_card=use_card)


def send_feishu_file(file_path, target_chat_id=None):
    feishu_send_file(file_path, build_feishu_context(), target_chat_id=target_chat_id)


# ── Feishu IM Layer: Chat Management ─────────────────────────────

def create_feishu_chat(name):
    return feishu_create_chat(name, build_feishu_context())


# ── 远程模式 ──────────────────────────────────────────

def build_remote_mode_context() -> RemoteModeContext:
    return RemoteModeContext(
        chat_session_map=chat_session_map,
        session_jsonl_id=session_jsonl_id,
        backend_display=backend_display,
        get_backend=get_backend,
        load_recent_history=load_recent_history,
        send_feishu_msg=send_feishu_msg,
    )


def enter_remote_mode(sname, chat_ids):
    remote_enter_remote_mode(sname, chat_ids, build_remote_mode_context())


def exit_remote_mode(sname, chat_ids, reason=""):
    remote_exit_remote_mode(sname, chat_ids, build_remote_mode_context(), reason=reason)


def ensure_remote_mode(sname):
    remote_ensure_remote_mode(sname, build_remote_mode_context())


# ── 命令处理 ──────────────────────────────────────────

def build_command_context() -> CommandContext:
    return CommandContext(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        allowed_user_id=ALLOWED_USER_ID,
        default_agent=DEFAULT_AGENT,
        claude_projects_dir=CLAUDE_PROJECTS_DIR,
        codex_sessions_dir=CODEX_SESSIONS_DIR,
        last_connect_time=last_connect_time,
        last_disconnect_time=last_disconnect_time,
        chat_session_map=chat_session_map,
        session_jsonl_id=session_jsonl_id,
        session_backend=session_backend,
        session_start_time=session_start_time,
        remote_mode=remote_mode,
        menu_notified=menu_notified,
        menu_state=menu_state,
        normalize_agent=normalize_agent,
        get_backend=get_backend,
        backend_display=backend_display,
        save_bindings=save_bindings,
        send_feishu_msg=send_feishu_msg,
        send_feishu_file=send_feishu_file,
        create_feishu_chat=create_feishu_chat,
        create_tmux_and_run=create_tmux_and_run,
        load_recent_history=load_recent_history,
        enter_remote_mode=enter_remote_mode,
        exit_remote_mode=exit_remote_mode,
        ensure_remote_mode=ensure_remote_mode,
        send_keys=send_keys,
        start_caffeinate=start_caffeinate,
        stop_caffeinate=stop_caffeinate,
        is_caffeinate_running=is_caffeinate_running,
    )


def handle_command(text, msg_chat_id):
    """处理用户发来的命令。实际路由在 commands.py。"""
    global _reply_chat_id
    _reply_chat_id = msg_chat_id
    route_command(text, msg_chat_id, build_command_context())


# ── JSONL 对话监控（后台线程）─────────────────────────────────


def build_monitor_context() -> MonitorContext:
    return MonitorContext(
        poll_interval=POLL_INTERVAL,
        capture_lines=CAPTURE_LINES,
        bridge_sent_window=BRIDGE_SENT_WINDOW,
        chat_session_map=chat_session_map,
        session_jsonl_id=session_jsonl_id,
        session_start_time=session_start_time,
        remote_mode=remote_mode,
        bridge_sent_time=bridge_sent_time,
        get_backend=get_backend,
        save_bindings=save_bindings,
        exit_remote_mode=exit_remote_mode,
        send_feishu_msg=send_feishu_msg,
        send_feishu_file=send_feishu_file,
    )


def jsonl_monitor():
    """后台监控 Claude/Codex JSONL；generic backend 降级为屏幕变化推送。"""
    run_jsonl_monitor(build_monitor_context())


# ── Feishu IM Layer: Inbound Message Handling ────────────────────

def on_message(data):
    feishu_on_message(data, build_feishu_context())


def catchup_missed_messages():
    feishu_catchup_missed_messages(build_feishu_context())


# ── 启动 ──────────────────────────────────────────────

def main():
    global lark_client

    if not APP_ID or not APP_SECRET:
        print("错误: 请在 .env 中配置 APP_ID 和 APP_SECRET")
        return
    if not ALLOWED_USER_ID and not ALLOW_ALL_USERS:
        print("错误: 请在 .env 中配置 ALLOWED_USER_ID；如确需允许所有用户，显式设置 ALLOW_ALL_USERS=true")
        return

    # caffeinate 不再自动启动，用户需要时通过 /caffeinate 开启
    atexit.register(stop_caffeinate)

    # 加载绑定关系
    load_bindings()

    # [IM-LAYER] Initialize Feishu/Lark API client and WebSocket connection.
    # To adapt to another platform, replace everything below with your IM's client setup.
    # Key integration points:
    #   1. Create API client for sending messages (lark_client)
    #   2. Register message event handler (on_message)
    #   3. Start WebSocket for real-time message receiving
    #   4. Hook connect/disconnect events for message recovery (catchup_missed_messages)
    lark_client = lark.Client.builder() \
        .app_id(APP_ID) \
        .app_secret(APP_SECRET) \
        .log_level(lark.LogLevel.INFO) \
        .build()

    # 注册消息事件处理器
    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .build()

    # 启动 JSONL 对话监控（读 Claude Code 的对话记录，比截屏干净）
    monitor_thread = threading.Thread(target=jsonl_monitor, daemon=True)
    monitor_thread.start()

    # 启动 WebSocket 长连接
    log.info("正在连接飞书...")
    log.info("连接成功后，在飞书私聊或群聊 tmux-bridge bot 发送 /help 查看用法")

    ws_client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )

    # 缩短重连等待：默认 0-30 秒随机延迟，改为 0-5 秒
    ws_client._reconnect_nonce = 5

    # 保存原始 _configure 方法，加日志打印服务端下发的连接配置
    _orig_configure = ws_client._configure
    def _logged_configure(conf):
        log.info(f"飞书服务端配置: ping_interval={conf.PingInterval}s, "
                 f"reconnect_interval={conf.ReconnectInterval}s, "
                 f"reconnect_nonce={conf.ReconnectNonce}s, "
                 f"reconnect_count={conf.ReconnectCount}")
        _orig_configure(conf)
        # 服务端配置会覆盖我们的 nonce，再次强制缩短
        ws_client._reconnect_nonce = min(ws_client._reconnect_nonce, 5)
    ws_client._configure = _logged_configure

    # 钩入 WebSocket 连接/断连事件，记录时间戳用于消息补拉
    _orig_connect = ws_client._connect
    async def _tracked_connect(*args, **kwargs):
        global last_connect_time
        result = await _orig_connect(*args, **kwargs)
        last_connect_time = time.time()
        # 重连后补拉断连期间的消息（在新线程中执行，不阻塞 WebSocket）
        if last_disconnect_time > 0:
            threading.Thread(
                target=catchup_missed_messages, daemon=True
            ).start()
        return result
    ws_client._connect = _tracked_connect

    _orig_disconnect = ws_client._disconnect
    async def _tracked_disconnect(*args, **kwargs):
        global last_disconnect_time
        last_disconnect_time = time.time()
        return await _orig_disconnect(*args, **kwargs)
    ws_client._disconnect = _tracked_disconnect

    ws_client.start()


if __name__ == "__main__":
    main()
