"""Application runtime wiring for pocket-claude/tmux-bridge."""

from __future__ import annotations

import atexit
import logging
import os
import ssl
import threading
import time

from dotenv import load_dotenv

load_dotenv()

from backends import CLAUDE_PROJECTS_DIR, CODEX_SESSIONS_DIR
from commands import CommandContext, handle_command as route_command
from feishu_adapter import FeishuAdapter
from history import load_recent_history
from im_adapter import IMAdapter, IMContext
from monitor import MonitorContext, jsonl_monitor as run_jsonl_monitor, menu_notified, menu_state
from remote_mode import (
    RemoteModeContext,
    remote_mode,
    enter_remote_mode as remote_enter_remote_mode,
    exit_remote_mode as remote_exit_remote_mode,
    ensure_remote_mode as remote_ensure_remote_mode,
)
from security import ALLOW_ALL_USERS, SKIP_SSL_VERIFY
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
from state import BridgeState, load_state, save_state

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

# ── 配置 ──────────────────────────────────────────────

APP_ID = os.getenv("APP_ID")
APP_SECRET = os.getenv("APP_SECRET")
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")

POLL_INTERVAL = 2
CAPTURE_LINES = 50
MAX_MSG_LEN = 4000
DEFAULT_AGENT = os.getenv("DEFAULT_AGENT", "claude").lower()
BRIDGE_SENT_WINDOW = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bridge")


class BridgeRuntime:
    """Owns bridge process state and wires modules together."""

    def __init__(self, im_adapter: IMAdapter | None = None):
        self.chat_session_map = {}
        self.session_jsonl_id = {}
        self.session_backend = {}
        self.session_runtime = {}
        self.session_start_time = {}
        self.im_client = None
        self.im_adapter = im_adapter or FeishuAdapter()
        self.reply_chat_id = None
        self.seen_message_ids = set()
        self.last_disconnect_time = 0
        self.last_connect_time = 0
        self.bridge_sent_time = {}

    # ── session runtime helpers ──────────────────────────────────────

    def build_session_runtime_context(self) -> SessionRuntimeContext:
        return SessionRuntimeContext(
            default_agent=DEFAULT_AGENT,
            session_backend=self.session_backend,
            bridge_sent_time=self.bridge_sent_time,
        )

    def normalize_agent(self, agent: str | None) -> str:
        return runtime_normalize_agent(agent, DEFAULT_AGENT)

    def get_backend(self, session_name: str | None) -> str:
        return runtime_get_backend(session_name, self.build_session_runtime_context())

    def backend_display(self, session_name: str | None) -> str:
        return runtime_backend_display(session_name, self.build_session_runtime_context())

    def send_keys(self, session: str, text: str):
        runtime_send_keys(session, text, self.build_session_runtime_context())

    def create_tmux_and_run(self, session_name, command):
        return runtime_create_tmux_and_run(session_name, command)

    # ── 绑定持久化 ────────────────────────────────────────────

    def load_bindings(self):
        loaded = load_state()
        self.chat_session_map = loaded.chat_session_map
        self.session_jsonl_id = loaded.session_jsonl_id
        self.session_backend = {k: self.normalize_agent(v) for k, v in loaded.session_backend.items()}
        self.session_runtime = loaded.session_runtime
        remote_mode.clear()
        remote_mode.update(loaded.remote_mode)
        log.info(f"已加载 {len(self.chat_session_map)} 个绑定")
        log.info(f"已加载 {len(self.session_jsonl_id)} 个 agent session ID")
        log.info(f"已加载 {len(self.session_backend)} 个 backend 绑定")

    def save_bindings(self):
        save_state(BridgeState(
            chat_session_map=self.chat_session_map,
            session_jsonl_id=self.session_jsonl_id,
            session_backend=self.session_backend,
            remote_mode=dict(remote_mode),
            session_runtime=self.session_runtime,
        ))

    # ── IM Adapter wrappers ──────────────────────────────

    def reset_disconnect_time(self):
        self.last_disconnect_time = 0

    def build_im_context(self) -> IMContext:
        return IMContext(
            client=self.im_client,
            default_chat_id=self.reply_chat_id,
            max_msg_len=MAX_MSG_LEN,
            allowed_user_id=ALLOWED_USER_ID,
            allow_all_users=ALLOW_ALL_USERS,
            seen_message_ids=self.seen_message_ids,
            chat_session_map=self.chat_session_map,
            remote_mode=remote_mode,
            last_disconnect_time=self.last_disconnect_time,
            last_connect_time=self.last_connect_time,
            handle_command=self.handle_command,
            reset_disconnect_time=self.reset_disconnect_time,
        )

    def build_feishu_context(self) -> IMContext:
        """Backward-compatible alias for tests/older integrations."""
        return self.build_im_context()

    def send_im_msg(self, text, target_chat_id=None, use_card=None):
        self.im_adapter.send_message(text, self.build_im_context(), target_chat_id=target_chat_id, use_card=use_card)

    def send_im_file(self, file_path, target_chat_id=None):
        self.im_adapter.send_file(file_path, self.build_im_context(), target_chat_id=target_chat_id)

    def create_im_chat(self, name):
        return self.im_adapter.create_chat(name, self.build_im_context())

    # ── 远程模式 ──────────────────────────────────────────

    def build_remote_mode_context(self) -> RemoteModeContext:
        return RemoteModeContext(
            chat_session_map=self.chat_session_map,
            session_jsonl_id=self.session_jsonl_id,
            backend_display=self.backend_display,
            get_backend=self.get_backend,
            load_recent_history=load_recent_history,
            send_im_msg=self.send_im_msg,
        )

    def enter_remote_mode(self, sname, chat_ids):
        remote_enter_remote_mode(sname, chat_ids, self.build_remote_mode_context())

    def exit_remote_mode(self, sname, chat_ids, reason=""):
        remote_exit_remote_mode(sname, chat_ids, self.build_remote_mode_context(), reason=reason)

    def ensure_remote_mode(self, sname):
        remote_ensure_remote_mode(sname, self.build_remote_mode_context())

    # ── 命令处理 ──────────────────────────────────────────

    def build_command_context(self) -> CommandContext:
        return CommandContext(
            app_id=APP_ID,
            app_secret=APP_SECRET,
            allowed_user_id=ALLOWED_USER_ID,
            default_agent=DEFAULT_AGENT,
            claude_projects_dir=CLAUDE_PROJECTS_DIR,
            codex_sessions_dir=CODEX_SESSIONS_DIR,
            last_connect_time=self.last_connect_time,
            last_disconnect_time=self.last_disconnect_time,
            chat_session_map=self.chat_session_map,
            session_jsonl_id=self.session_jsonl_id,
            session_backend=self.session_backend,
            session_start_time=self.session_start_time,
            remote_mode=remote_mode,
            menu_notified=menu_notified,
            menu_state=menu_state,
            normalize_agent=self.normalize_agent,
            get_backend=self.get_backend,
            backend_display=self.backend_display,
            save_bindings=self.save_bindings,
            send_im_msg=self.send_im_msg,
            send_im_file=self.send_im_file,
            create_im_chat=self.create_im_chat,
            create_tmux_and_run=self.create_tmux_and_run,
            load_recent_history=load_recent_history,
            enter_remote_mode=self.enter_remote_mode,
            exit_remote_mode=self.exit_remote_mode,
            ensure_remote_mode=self.ensure_remote_mode,
            send_keys=self.send_keys,
            start_caffeinate=start_caffeinate,
            stop_caffeinate=stop_caffeinate,
            is_caffeinate_running=is_caffeinate_running,
        )

    def handle_command(self, text, msg_chat_id):
        self.reply_chat_id = msg_chat_id
        route_command(text, msg_chat_id, self.build_command_context())

    # ── JSONL 对话监控（后台线程）─────────────────────────────────

    def build_monitor_context(self) -> MonitorContext:
        return MonitorContext(
            poll_interval=POLL_INTERVAL,
            capture_lines=CAPTURE_LINES,
            bridge_sent_window=BRIDGE_SENT_WINDOW,
            chat_session_map=self.chat_session_map,
            session_jsonl_id=self.session_jsonl_id,
            session_runtime=self.session_runtime,
            session_start_time=self.session_start_time,
            remote_mode=remote_mode,
            bridge_sent_time=self.bridge_sent_time,
            get_backend=self.get_backend,
            save_bindings=self.save_bindings,
            exit_remote_mode=self.exit_remote_mode,
            send_im_msg=self.send_im_msg,
            send_im_file=self.send_im_file,
        )

    def jsonl_monitor(self):
        """后台监控 Claude/Codex JSONL；generic backend 降级为屏幕变化推送。"""
        run_jsonl_monitor(self.build_monitor_context())

    # ── IM Layer: Inbound Message Handling ────────────────────

    def on_message(self, data):
        self.im_adapter.on_message(data, self.build_im_context())

    def catchup_missed_messages(self):
        self.im_adapter.catchup_missed_messages(self.build_im_context())

    # ── 启动 ──────────────────────────────────────────────

    def run(self):
        # Import the Feishu SDK only when starting the bridge, so unit tests can
        # import BridgeRuntime without requiring the optional runtime dependency.
        import lark_oapi as lark

        if not APP_ID or not APP_SECRET:
            print("错误: 请在 .env 中配置 APP_ID 和 APP_SECRET")
            return
        if not ALLOWED_USER_ID and not ALLOW_ALL_USERS:
            print("错误: 请在 .env 中配置 ALLOWED_USER_ID；如确需允许所有用户，显式设置 ALLOW_ALL_USERS=true")
            return

        # caffeinate 不再自动启动，用户需要时通过 /caffeinate 开启
        atexit.register(stop_caffeinate)

        # 加载绑定关系
        self.load_bindings()

        # [IM-LAYER] Initialize Feishu/Lark API client and WebSocket connection.
        self.im_client = lark.Client.builder() \
            .app_id(APP_ID) \
            .app_secret(APP_SECRET) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        # 注册消息事件处理器
        handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self.on_message) \
            .build()

        # 启动 JSONL 对话监控（读 Claude Code 的对话记录，比截屏干净）
        monitor_thread = threading.Thread(target=self.jsonl_monitor, daemon=True)
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
            result = await _orig_connect(*args, **kwargs)
            self.last_connect_time = time.time()
            # 重连后补拉断连期间的消息（在新线程中执行，不阻塞 WebSocket）
            if self.last_disconnect_time > 0:
                threading.Thread(
                    target=self.catchup_missed_messages, daemon=True
                ).start()
            return result

        ws_client._connect = _tracked_connect

        _orig_disconnect = ws_client._disconnect

        async def _tracked_disconnect(*args, **kwargs):
            self.last_disconnect_time = time.time()
            return await _orig_disconnect(*args, **kwargs)

        ws_client._disconnect = _tracked_disconnect

        ws_client.start()


def main():
    BridgeRuntime().run()
