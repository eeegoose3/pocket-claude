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
import hashlib
import json
import logging
import os
import re
import signal
import ssl
import subprocess
import threading
import time

from dotenv import load_dotenv

load_dotenv()

from backends import (
    CLAUDE_PROJECTS_DIR,
    CODEX_SESSIONS_DIR,
    backend_display as backend_display_for_agent,
    find_log_by_session_id,
    infer_backend_from_command,
    jsonl_candidates_for_agent,
    normalize_agent as normalize_agent_value,
)
from formatting import clean_ansi, convert_tables_in_text, has_markdown
from commands import CommandContext, handle_command as route_command, parse_menu_options
from state import BridgeState, load_state, save_state
from tmux import (
    capture_pane,
    send_keys as tmux_send_keys,
    tmux_run,
)
from security import (
    ALLOW_ALL_USERS,
    SKIP_SSL_VERIFY,
    whitelist_allows_sender,
)
from parsers import (
    check_tool_result,
    extract_assistant_text,
    extract_image_write,
    extract_interactive_ui,
    extract_screenshot_path,
    extract_system_event,
    extract_user_text,
    is_turn_complete,
    session_id_from_log_path,
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
from lark_oapi.api.im.v1 import (
    CreateMessageRequest, CreateMessageRequestBody,   # Send text/card messages
    CreateChatRequest, CreateChatRequestBody,          # Create group chats
    CreateImageRequest, CreateImageRequestBody,        # Upload images
    CreateFileRequest, CreateFileRequestBody,          # Upload files
    ListMessageRequest,                                # Pull message history (for reconnect recovery)
)

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
caffeinate_proc = None      # caffeinate 子进程，阻止 Mac 睡眠
last_disconnect_time = 0    # WebSocket 上次断连时间戳
last_connect_time = 0       # WebSocket 上次连接时间戳
remote_mode = {}            # {session_name: bool} 远程模式（True = 推送到飞书）
bridge_sent_time = {}       # {session_name: float} bridge 最近一次向 CLI 发送的时间
BRIDGE_SENT_WINDOW = 15     # 秒，此窗口内的日志 user 消息视为 bridge 发送


# ── CLI backend 抽象 ───────────────────────────────────────────


def normalize_agent(agent: str | None) -> str:
    return normalize_agent_value(agent, DEFAULT_AGENT)


def get_backend(session_name: str | None) -> str:
    """Return backend for a tmux session, inferring from the running pane if needed."""
    if session_name and session_name in session_backend:
        return normalize_agent(session_backend[session_name])

    inferred = None
    if session_name:
        ok, pane_cmd = tmux_run(["display-message", "-t", session_name, "-p", "#{pane_current_command}"])
        if ok:
            inferred = infer_backend_from_command(pane_cmd, DEFAULT_AGENT)
    backend = normalize_agent(inferred or DEFAULT_AGENT)
    if session_name:
        session_backend[session_name] = backend
    return backend


def backend_display(session_name: str | None) -> str:
    return backend_display_for_agent(get_backend(session_name))



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

# ── caffeinate 防睡眠 ───────────────────────────────────────

def start_caffeinate():
    """启动 caffeinate 阻止系统睡眠（允许屏幕关闭）"""
    global caffeinate_proc
    try:
        caffeinate_proc = subprocess.Popen(
            ["caffeinate", "-s"],  # -s: prevent system sleep on AC power
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info(f"caffeinate 已启动 (PID {caffeinate_proc.pid})，Mac 不会自动睡眠")
    except Exception as e:
        log.error(f"caffeinate 启动失败: {e}")

def stop_caffeinate():
    """停止 caffeinate，恢复正常睡眠"""
    global caffeinate_proc
    if caffeinate_proc:
        caffeinate_proc.terminate()
        caffeinate_proc.wait()
        log.info("caffeinate 已停止")
        caffeinate_proc = None

# ── tmux 操作 ──────────────────────────────────────────

def send_keys(session: str, text: str):
    """向 tmux session 发送按键并记录 bridge 最近发送时间。"""
    bridge_sent_time[session] = time.time()
    tmux_send_keys(session, text)


# ── Feishu IM Layer: Message Sending ──────────────────────────────
# [IM-LAYER] Replace send_feishu_msg() and send_feishu_file() to adapt to other platforms.
# Key behaviors to preserve:
#   - Long message splitting (MAX_MSG_LEN)
#   - Markdown table → vertical list conversion for mobile readability
#   - Image upload as inline preview (not file attachment)

def send_feishu_msg(text, target_chat_id=None, use_card=None):
    """[IM-LAYER] Send a text or card message to Feishu.

    This is the primary outbound messaging function. All push notifications,
    command responses, and forwarded Claude output go through here.

    Args:
        text: Message content (plain text or markdown)
        target_chat_id: Feishu chat ID to send to (defaults to current command's chat)
        use_card: True=force card (markdown), None=auto-detect, False=force plain text
    """
    cid = target_chat_id or _reply_chat_id
    if not cid or not lark_client:
        log.warning("chat_id 或 lark_client 未初始化，无法发送消息")
        return

    # 自动检测是否需要卡片
    if use_card is None:
        use_card = has_markdown(text)

    # 分条发送超长消息
    chunks = []
    while len(text) > MAX_MSG_LEN:
        split_pos = text.rfind("\n", 0, MAX_MSG_LEN)
        if split_pos == -1:
            split_pos = MAX_MSG_LEN
        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip("\n")
    if text:
        chunks.append(text)

    for chunk in chunks:
        if use_card:
            msg_type = "interactive"
            # 把 markdown 表格转成纵向列表，适合手机阅读
            chunk = convert_tables_in_text(chunk)
            content = json.dumps({
                "config": {"wide_screen_mode": True},
                "elements": [{"tag": "markdown", "content": chunk}],
            })
        else:
            msg_type = "text"
            content = json.dumps({"text": chunk})

        body = CreateMessageRequestBody.builder() \
            .msg_type(msg_type) \
            .receive_id(cid) \
            .content(content) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(body) \
            .build()
        try:
            resp = lark_client.im.v1.message.create(req)
            if not resp.success():
                log.error(f"发送消息失败: {resp.code} {resp.msg}")
        except Exception as e:
            log.error(f"发送消息异常: {e}")


# 图片扩展名集合
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}

# 飞书文件类型映射
FILE_TYPE_MAP = {
    ".pdf": "pdf", ".mp4": "mp4", ".mp3": "mp3",
    ".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx",
    ".doc": "doc",
}


def send_feishu_file(file_path, target_chat_id=None):
    """[IM-LAYER] Upload a local file and send it to Feishu.

    Images (.png, .jpg, etc.) are uploaded as inline images (directly visible in chat).
    Other files are uploaded as downloadable attachments.
    Used by: /file command, image auto-push in remote mode.
    """
    cid = target_chat_id or _reply_chat_id
    if not cid or not lark_client:
        log.warning("chat_id 或 lark_client 未初始化，无法发送文件")
        return

    file_path = os.path.expanduser(file_path)
    if not os.path.isfile(file_path):
        send_feishu_msg(f"文件不存在: {file_path}", target_chat_id=cid)
        return

    ext = os.path.splitext(file_path)[1].lower()
    file_name = os.path.basename(file_path)

    try:
        if ext in IMAGE_EXTS:
            # 图片：上传后用 image 消息发送（手机可直接预览）
            with open(file_path, "rb") as f:
                body = CreateImageRequestBody.builder() \
                    .image_type("message") \
                    .image(f) \
                    .build()
                req = CreateImageRequest.builder() \
                    .request_body(body) \
                    .build()
                resp = lark_client.im.v1.image.create(req)

            if not resp.success():
                send_feishu_msg(f"图片上传失败: {resp.code} {resp.msg}", target_chat_id=cid)
                return

            content = json.dumps({"image_key": resp.data.image_key})
            msg_body = CreateMessageRequestBody.builder() \
                .msg_type("image") \
                .receive_id(cid) \
                .content(content) \
                .build()
        else:
            # 其他文件：上传后用 file 消息发送（手机可下载）
            file_type = FILE_TYPE_MAP.get(ext, "stream")
            with open(file_path, "rb") as f:
                body = CreateFileRequestBody.builder() \
                    .file_type(file_type) \
                    .file_name(file_name) \
                    .file(f) \
                    .build()
                req = CreateFileRequest.builder() \
                    .request_body(body) \
                    .build()
                resp = lark_client.im.v1.file.create(req)

            if not resp.success():
                send_feishu_msg(f"文件上传失败: {resp.code} {resp.msg}", target_chat_id=cid)
                return

            content = json.dumps({"file_key": resp.data.file_key})
            msg_body = CreateMessageRequestBody.builder() \
                .msg_type("file") \
                .receive_id(cid) \
                .content(content) \
                .build()

        msg_req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(msg_body) \
            .build()
        msg_resp = lark_client.im.v1.message.create(msg_req)
        if msg_resp.success():
            send_feishu_msg(f"✅ {file_name}", target_chat_id=cid)
        else:
            log.error(f"发送文件消息失败: {msg_resp.code} {msg_resp.msg}")
    except Exception as e:
        log.error(f"文件发送异常: {e}")
        send_feishu_msg(f"文件发送失败: {e}", target_chat_id=cid)


# ── tmux session 创建 ────────────────────────────────────────





def create_tmux_and_run(session_name, command):
    """创建 tmux session 并在里面执行命令"""
    ok, _ = tmux_run(["new-session", "-d", "-s", session_name])
    if not ok:
        return False, f"创建 tmux session '{session_name}' 失败（可能已存在）"
    tmux_run(["send-keys", "-t", session_name, "--", command])
    tmux_run(["send-keys", "-t", session_name, "Enter"])
    return True, ""


# ── Feishu IM Layer: Chat Management ─────────────────────────────

def create_feishu_chat(name):
    """[IM-LAYER] Create a new Feishu group chat and add the user to it.

    Each tmux session gets its own group chat for isolated communication.
    The bot is set as chat manager so it can send messages without being @mentioned.
    Returns the new chat_id, or None on failure.
    """
    if not lark_client or not ALLOWED_USER_ID:
        return None
    body = CreateChatRequestBody.builder() \
        .name(name) \
        .user_id_list([ALLOWED_USER_ID]) \
        .build()
    req = CreateChatRequest.builder() \
        .user_id_type("open_id") \
        .set_bot_manager(True) \
        .request_body(body) \
        .build()
    try:
        resp = lark_client.im.v1.chat.create(req)
        if resp.success():
            log.info(f"创建群聊成功: {name} -> {resp.data.chat_id}")
            return resp.data.chat_id
        else:
            log.error(f"创建群聊失败: {resp.code} {resp.msg}")
            return None
    except Exception as e:
        log.error(f"创建群聊异常: {e}")
        return None


# ── 远程模式 ──────────────────────────────────────────

def enter_remote_mode(sname, chat_ids):
    """进入远程模式：推送最近 3 轮对话上下文到飞书"""
    remote_mode[sname] = True
    log.info(f"[远程模式] {sname} 进入远程模式")
    display = backend_display(sname)
    sid = session_jsonl_id.get(sname)
    if sid:
        history = load_recent_history(sid, agent=get_backend(sname))
        if history:
            for cid in chat_ids:
                send_feishu_msg("── 📱 进入远程模式，以下是最近对话 ──", target_chat_id=cid, use_card=False)
            for msg in history:
                if msg["role"] == "user":
                    for cid in chat_ids:
                        send_feishu_msg(f"👤 你：{msg['text']}", target_chat_id=cid, use_card=False)
                else:
                    t = msg["text"]
                    if len(t) > 500:
                        t = t[:500] + "...（已截断）"
                    for cid in chat_ids:
                        send_feishu_msg(f"🤖 {display}：{t}", target_chat_id=cid, use_card=True)
            for cid in chat_ids:
                send_feishu_msg("── 以上是历史，以下是实时 ──", target_chat_id=cid, use_card=False)
            return
    for cid in chat_ids:
        send_feishu_msg("📱 已进入远程模式", target_chat_id=cid)


def exit_remote_mode(sname, chat_ids, reason=""):
    """退出远程模式"""
    remote_mode[sname] = False
    log.info(f"[远程模式] {sname} 退出远程模式: {reason}")
    msg = "💻 已切换到本地模式"
    if reason:
        msg += f"（{reason}）"
    for cid in chat_ids:
        send_feishu_msg(msg, target_chat_id=cid)


def ensure_remote_mode(sname):
    """如果 session 不在远程模式，自动进入"""
    if not remote_mode.get(sname, False):
        chat_ids = [cid for cid, sn in chat_session_map.items() if sn == sname]
        enter_remote_mode(sname, chat_ids)


# ── 命令处理 ──────────────────────────────────────────

def is_caffeinate_running() -> bool:
    return bool(caffeinate_proc and caffeinate_proc.poll() is None)


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

def verify_jsonl_by_screen(session_name, candidate_files):
    """通过 tmux 屏幕内容验证哪个 JSONL 文件属于这个 session。
    从每个候选 JSONL 的尾部提取最近的 assistant 文本片段，
    与 tmux 屏幕上显示的内容做交叉比对。
    """
    # 获取屏幕内容
    ok, screen = tmux_run(["capture-pane", "-t", session_name, "-p"])
    if not ok or not screen or len(screen.strip()) < 20:
        return None

    screen_text = screen.strip()
    matched = []

    for fpath in candidate_files:
        try:
            # 从文件尾部读取最后 100 行，提取 assistant 文本指纹
            with open(fpath, "r") as f:
                # 用 deque 高效读尾部
                from collections import deque
                tail_lines = deque(f, maxlen=100)

            fingerprints = []
            for line in tail_lines:
                text = extract_assistant_text(line)
                if text:
                    # 取前 80 个字符作为指纹（去掉 markdown 符号和空白）
                    clean = re.sub(r"[#*`_\-|>\s]+", " ", text).strip()
                    if len(clean) >= 15:
                        fingerprints.append(clean[:80])

            if not fingerprints:
                continue

            # 检查最近 3 条指纹是否有任一出现在屏幕中
            for fp in fingerprints[-3:]:
                # 指纹也做同样的清理
                screen_clean = re.sub(r"[#*`_\-|>\s]+", " ", screen_text)
                if fp in screen_clean:
                    matched.append(fpath)
                    break
        except Exception as e:
            log.debug(f"验证 JSONL 屏幕匹配失败 {fpath}: {e}")
            continue

    if len(matched) == 1:
        log.info(f"屏幕验证命中: {session_name} → {os.path.basename(matched[0])}")
        return matched[0]

    if len(matched) > 1:
        log.warning(f"屏幕验证多个命中 ({len(matched)})，回退到时间排序")
    else:
        log.debug(f"屏幕验证无命中，回退到时间排序")
    return None






def find_jsonl_for_session(session_name):
    """找到 tmux session 中当前 CLI backend 正在写入的 JSONL 对话文件。

    Claude Code: ~/.claude/projects/<project>/*.jsonl
    Codex:       ~/.codex/sessions/YYYY/MM/DD/*.jsonl
    Generic CLI: no structured log, returns None and falls back to screen monitor.
    """
    agent = get_backend(session_name)
    if agent == "generic":
        return None

    # 优先用已知的 session_id 精确匹配（/resume 时记录的）
    known_id = session_jsonl_id.get(session_name)
    if known_id:
        match = find_log_by_session_id(known_id, agent)
        if match and os.path.exists(match):
            return match

    # 获取 session 的工作目录
    ok, cwd = tmux_run(["display-message", "-t", session_name, "-p", "#{pane_current_path}"])
    if not ok or not cwd:
        return None

    jsonl_files = jsonl_candidates_for_agent(agent, cwd)
    if not jsonl_files:
        return None

    # 如果有启动时间（/start 创建的），只看启动后创建/修改的文件
    start_ts = session_start_time.get(session_name)
    if start_ts:
        new_files = [f for f in jsonl_files if os.path.getmtime(f) > start_ts]
        if new_files:
            target = max(new_files, key=os.path.getmtime)
            # 找到了，锁定它，以后不会再变
            sid = session_id_from_log_path(target, agent)
            session_jsonl_id[session_name] = sid
            session_start_time.pop(session_name, None)
            save_bindings()
            log.info(f"自动锁定 JSONL: {session_name} → {sid}")
            return target
        # 还没出现新文件（Claude Code 还在启动），等下次轮询
        return None

    # 兜底：仅用于 /new 或 /bind 等没有精确信息的场景
    # 排除已被其他 session 占用的 JSONL，避免同目录下多 session 互相抢文件
    claimed_ids = set(session_jsonl_id.values())
    unclaimed = [f for f in jsonl_files
                 if session_id_from_log_path(f, agent) not in claimed_ids]
    candidates = unclaimed if unclaimed else jsonl_files

    # 优先：屏幕内容交叉验证（精准匹配）
    verified = verify_jsonl_by_screen(session_name, candidates)
    if verified:
        sid = session_id_from_log_path(verified, agent)
        session_jsonl_id[session_name] = sid
        save_bindings()
        log.info(f"屏幕验证锁定 JSONL: {session_name} → {sid}")
        return verified

    # 降级：取最近修改的
    latest = max(candidates, key=os.path.getmtime)
    if time.time() - os.path.getmtime(latest) > 300:
        return None
    sid = session_id_from_log_path(latest, agent)
    session_jsonl_id[session_name] = sid
    save_bindings()
    log.info(f"时间排序锁定 JSONL: {session_name} → {sid}")
    return latest




















def load_recent_history(session_id, rounds=3, agent: str | None = None):
    """从 JSONL 文件中读取最近 N 轮对话（1 轮 = 1 条 user + 1 条 assistant）。
    返回按时间正序排列的列表：[{"role": "user", "text": "..."}, {"role": "assistant", "text": "..."}, ...]
    """
    # 定位 JSONL 文件
    jsonl_path = find_log_by_session_id(session_id, agent)
    if not jsonl_path:
        log.warning(f"load_recent_history: 找不到 {session_id}.jsonl")
        return []

    # 读取所有行，提取 user 和 assistant 消息
    messages = []  # [(index, role, text), ...]
    try:
        with open(jsonl_path, "r") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                user_text = extract_user_text(line)
                if user_text:
                    messages.append((i, "user", user_text))
                    continue
                assistant_text = extract_assistant_text(line)
                if assistant_text:
                    messages.append((i, "assistant", assistant_text))
    except Exception as e:
        log.error(f"load_recent_history: 读取 JSONL 失败: {e}")
        return []

    if not messages:
        return []

    # 从后往前配对：找最近的 rounds 轮（1 轮 = 1 user + 1 assistant）
    rounds_collected = []  # [(user_msg, assistant_msg), ...]
    idx = len(messages) - 1
    while idx >= 0 and len(rounds_collected) < rounds:
        # 先找一条 assistant
        while idx >= 0 and messages[idx][1] != "assistant":
            idx -= 1
        if idx < 0:
            break
        assistant_msg = messages[idx]
        idx -= 1
        # 再找一条 user
        while idx >= 0 and messages[idx][1] != "user":
            idx -= 1
        if idx < 0:
            break
        user_msg = messages[idx]
        idx -= 1
        rounds_collected.append((user_msg, assistant_msg))

    # 翻转为时间正序（按轮翻转，保持每轮内 user→assistant 顺序）
    rounds_collected.reverse()
    result = []
    for user_msg, assistant_msg in rounds_collected:
        result.append({"role": "user", "text": user_msg[2]})
        result.append({"role": "assistant", "text": assistant_msg[2]})
    return result


# 每个 session 的 JSONL 监控状态
jsonl_state = {}  # {session_name: {"path": str, "pos": int, "last_change": float}}
screen_state = {}  # {session_name: {"hash": str, "text": str, "time": float}} generic/screen fallback
plan_notified = set()  # 已推送过 plan 通知的 session，避免重复推送
menu_notified = set()  # 已推送过选择菜单通知的 session
menu_state = {}        # {session_name: [{"num": int, "text": str}, ...]} 当前活跃的选择菜单
# 权限确认追踪：{session_name: {"id": tool_use_id, "name": "Bash", "detail": "...", "time": timestamp}}
pending_permission = {}
pending_image = {}     # {session_name: {"id": tool_use_id, "path": str, "time": timestamp}}
PERMISSION_WAIT = 3    # 秒，tool_use 后等多久没有 tool_result 就判定为等权限确认
STALE_THRESHOLD = 60   # 秒，JSONL 文件超过此时间无变化则检查是否切换了会话
SCREEN_PUSH_MIN_INTERVAL = 5  # generic backend 屏幕变化最小推送间隔


def maybe_push_screen_update(sname, chat_ids, is_remote):
    """Generic CLI fallback: push cleaned tmux screen when it changes in remote mode."""
    screen = clean_ansi(capture_pane(sname, lines=CAPTURE_LINES)).strip()
    if not screen:
        return
    digest = hashlib.sha1(screen.encode("utf-8", errors="ignore")).hexdigest()
    state = screen_state.get(sname, {})
    if state.get("hash") == digest:
        return
    screen_state[sname] = {"hash": digest, "text": screen, "time": time.time()}
    if not is_remote:
        return
    last_push = state.get("push_time", 0)
    if time.time() - last_push < SCREEN_PUSH_MIN_INTERVAL:
        return
    screen_state[sname]["push_time"] = time.time()
    text = screen
    if len(text) > 3000:
        text = text[-3000:]
    for cid in chat_ids:
        send_feishu_msg(f"📺 {sname} 屏幕更新:\n{text}", target_chat_id=cid, use_card=False)


def find_continuation_jsonl(current_jsonl_path):
    """当 Claude Code 清除上下文后，通过旧 session ID 找到延续的新 JSONL 文件"""
    current_dir = os.path.dirname(current_jsonl_path)
    current_basename = os.path.basename(current_jsonl_path)
    current_session_id = os.path.splitext(current_basename)[0]
    current_mtime = os.path.getmtime(current_jsonl_path)

    # 只检查比当前文件更新的 JSONL 文件
    candidates = []
    try:
        for f in os.listdir(current_dir):
            if not f.endswith(".jsonl") or f == current_basename:
                continue
            fpath = os.path.join(current_dir, f)
            if os.path.getmtime(fpath) > current_mtime:
                candidates.append(fpath)
    except OSError:
        return None

    if not candidates:
        return None

    # 在候选文件的前 10 行中搜索旧 session ID（必须是 sessionId 字段，不是对话内容）
    needle = f'"sessionId":"{current_session_id}"'
    needle_spaced = f'"sessionId": "{current_session_id}"'
    for fpath in sorted(candidates, key=os.path.getmtime, reverse=True):
        try:
            with open(fpath, "r") as fp:
                for i, line in enumerate(fp):
                    if i >= 10:
                        break
                    if needle in line or needle_spaced in line:
                        return fpath
        except Exception:
            continue

    return None


def jsonl_monitor():
    """后台监控 Claude/Codex JSONL；generic backend 降级为屏幕变化推送。"""
    global jsonl_state

    log.info("对话日志/屏幕监控已启动")
    while True:
        try:
            time.sleep(POLL_INTERVAL)

            if not chat_session_map:
                continue

            # session → [chat_id, ...] 映射
            session_chats = {}
            for cid, sname in chat_session_map.items():
                session_chats.setdefault(sname, []).append(cid)

            for sname, chat_ids in session_chats.items():
                agent = get_backend(sname)
                is_remote = remote_mode.get(sname, False)

                # Generic CLI 没有结构化 JSONL：远程模式下用屏幕变化作为通用 fallback。
                if agent == "generic":
                    maybe_push_screen_update(sname, chat_ids, is_remote)
                    continue

                # 查找或更新 JSONL 文件路径
                state = jsonl_state.get(sname)

                if not state:
                    jsonl_path = find_jsonl_for_session(sname)
                    if not jsonl_path:
                        # Agent may be on a startup/login/model-picker screen before logs exist.
                        maybe_push_screen_update(sname, chat_ids, is_remote)
                        continue
                    # 从文件末尾开始（不发送历史消息）
                    pos = os.path.getsize(jsonl_path)
                    jsonl_state[sname] = {"path": jsonl_path, "pos": pos, "last_change": time.time()}
                    log.info(f"监控 {agent} JSONL: {sname} → {os.path.basename(jsonl_path)}")
                    continue

                jsonl_path = state["path"]
                pos = state["pos"]

                # 检查文件是否还存在
                if not os.path.exists(jsonl_path):
                    jsonl_state.pop(sname, None)
                    session_jsonl_id.pop(sname, None)  # 文件没了，解除锁定
                    continue

                # 读取新增内容
                current_size = os.path.getsize(jsonl_path)
                if current_size > pos:
                    with open(jsonl_path, "r") as f:
                        f.seek(pos)
                        new_content = f.read()

                    jsonl_state[sname] = {"path": jsonl_path, "pos": current_size, "last_change": time.time()}

                    # 解析 JSONL 新内容：user 消息检测 + assistant 回复 + 交互式 UI + 系统事件 + 权限确认
                    for line in new_content.strip().split("\n"):
                        if not line.strip():
                            continue

                        # ⓪ user 消息检测（远程模式：检测本地键盘输入 → 自动退出）
                        user_text = extract_user_text(line)
                        if user_text and is_remote:
                            if time.time() - bridge_sent_time.get(sname, 0) > BRIDGE_SENT_WINDOW:
                                # 本地键盘输入 → 推送内容 + 退出远程模式
                                for cid in chat_ids:
                                    send_feishu_msg(f"👤 本地输入：{user_text}", target_chat_id=cid, use_card=False)
                                exit_remote_mode(sname, chat_ids, "检测到本地键盘输入")
                                is_remote = False

                        # ① assistant 文本回复 → 推送到飞书（仅远程模式）
                        text = extract_assistant_text(line)
                        if text and is_remote:
                            for cid in chat_ids:
                                send_feishu_msg(text, target_chat_id=cid, use_card=True)

                        # ② 交互式 UI 检测（AskUserQuestion / ExitPlanMode / 权限确认）
                        ui = extract_interactive_ui(line)
                        if ui:
                            if ui["type"] == "ask" and sname not in menu_notified:
                                # AskUserQuestion：推送带描述的选项到飞书
                                if is_remote:
                                    for q in ui["questions"]:
                                        options = q.get("options", [])
                                        if not options:
                                            continue
                                        msg = f"📋 {q.get('question', '请选择')}：\n\n"
                                        for i, opt in enumerate(options, 1):
                                            msg += f"{i}. {opt.get('label', '')}"
                                            desc = opt.get("description", "")
                                            if desc:
                                                msg += f"\n   {desc}"
                                            msg += "\n"
                                        msg += "\n或直接发文字自定义回复"
                                        for cid in chat_ids:
                                            send_feishu_msg(msg, target_chat_id=cid)
                                # 状态追踪始终执行（不管是否远程）
                                first_q = ui["questions"][0] if ui["questions"] else {}
                                opts = first_q.get("options", [])
                                if opts:
                                    menu_state[sname] = [{"num": i + 1, "text": o["label"]} for i, o in enumerate(opts)]
                                    menu_notified.add(sname)
                                    log.info(f"[JSONL] 检测到选择菜单: {sname}, {len(opts)} 个选项, remote={is_remote}")

                            elif ui["type"] == "plan_exit" and sname not in plan_notified:
                                # ExitPlanMode：从 JSONL 直接提取计划内容
                                if is_remote:
                                    plan_text = ui.get("plan")
                                    if plan_text:
                                        if len(plan_text) > 2000:
                                            plan_text = plan_text[:2000] + "\n\n...（已截断）"
                                        for cid in chat_ids:
                                            send_feishu_msg(f"📋 **Plan 内容：**\n\n{plan_text}", target_chat_id=cid, use_card=True)
                                plan_notified.add(sname)
                                log.info(f"[JSONL] 检测到 plan: {sname}, remote={is_remote}")

                            elif ui["type"] == "tool_pending":
                                # 可能需要权限确认的工具调用，记录并等 PERMISSION_WAIT 秒
                                pending_permission[sname] = {
                                    "id": ui["id"], "name": ui["name"],
                                    "detail": ui["detail"], "time": time.time(),
                                }

                        # ②b 图片写入检测 — 记录待确认的图片文件
                        img = extract_image_write(line)
                        if img:
                            pending_image[sname] = {
                                "id": img["tool_id"], "path": img["path"], "time": time.time(),
                            }

                        # ③ tool_result 到达 → 清除权限等待状态 + 推送图片
                        if sname in pending_permission:
                            if check_tool_result(line, pending_permission[sname]["id"]):
                                pending_permission.pop(sname, None)
                        if sname in pending_image:
                            pi = pending_image[sname]
                            if check_tool_result(line, pi["id"]):
                                # Write 图片工具执行成功，自动推送
                                if is_remote:
                                    img_path = pi["path"]
                                    if img_path == "__screenshot__":
                                        # Playwright 截图：从 tool_result 中提取路径
                                        real_path = extract_screenshot_path(line, pi["id"])
                                        if real_path:
                                            img_path = real_path
                                        else:
                                            img_path = None
                                    if img_path and os.path.isfile(img_path):
                                        log.info(f"[图片推送] {sname} → {img_path}")
                                        for cid in chat_ids:
                                            send_feishu_file(img_path, target_chat_id=cid)
                                pending_image.pop(sname, None)

                        # ④ 系统事件（上下文压缩、API 错误）— 仅远程模式推送
                        evt = extract_system_event(line)
                        if evt and is_remote:
                            if evt["type"] == "compact":
                                tokens = evt["pre_tokens"]
                                if tokens > 0:
                                    for cid in chat_ids:
                                        send_feishu_msg(f"🗜️ 上下文已压缩（压缩前 {tokens:,} tokens）", target_chat_id=cid)
                            elif evt["type"] == "api_error" and evt["retry_attempt"] >= 3:
                                for cid in chat_ids:
                                    send_feishu_msg(
                                        f"⚠️ API 错误，第 {evt['retry_attempt']}/{evt['max_retries']} 次重试",
                                        target_chat_id=cid,
                                    )

                    # ⑤ 回复完毕通知（仅远程模式）
                    if is_remote:
                        lines = [l for l in new_content.strip().split("\n") if l.strip()]
                        if lines and is_turn_complete(lines[-1]):
                            for cid in chat_ids:
                                send_feishu_msg("✅ 已回复完毕，等待指令", target_chat_id=cid)
                else:
                    # 文件无变化，检查是否会话切换（clear context 等）
                    last_change = state.get("last_change", time.time())
                    if time.time() - last_change > STALE_THRESHOLD:
                        new_path = find_continuation_jsonl(jsonl_path)
                        if new_path:
                            new_sid = session_id_from_log_path(new_path, agent)
                            session_jsonl_id[sname] = new_sid
                            save_bindings()
                            # 从新文件末尾开始（跳过已有内容）
                            new_size = os.path.getsize(new_path)
                            jsonl_state[sname] = {"path": new_path, "pos": new_size, "last_change": time.time()}
                            log.info(f"会话切换: {sname} → {os.path.basename(new_path)}")
                            if is_remote:
                                for cid in chat_ids:
                                    send_feishu_msg(f"⚠️ 检测到 {sname} 会话已切换（clear context），已自动跟踪新会话", target_chat_id=cid)

                # ── 权限确认超时检测（仅远程模式推送）──
                if sname in pending_permission:
                    pp = pending_permission[sname]
                    if time.time() - pp["time"] > PERMISSION_WAIT:
                        tool_name = pp["name"]
                        detail = pp["detail"]
                        if is_remote:
                            if tool_name == "Bash":
                                hint = f"🔐 等待确认：{sname} 要执行命令\n\n`{detail}`\n\n发 /y 批准 · /n 拒绝"
                            elif tool_name == "exec_command":
                                hint = f"🔐 等待确认：{sname} 要执行命令/提权操作\n\n{detail}\n\n发 /y 批准 · /n 拒绝"
                            elif tool_name == "apply_patch":
                                hint = f"🔐 等待确认：{sname} 要修改文件\n\n{detail}\n\n发 /y 批准 · /n 拒绝"
                            elif tool_name == "Edit":
                                hint = f"🔐 等待确认：{sname} 要编辑文件\n\n{detail}\n\n发 /y 批准 · /n 拒绝"
                            else:
                                hint = f"🔐 等待确认：{sname} 要写入文件\n\n{detail}\n\n发 /y 批准 · /n 拒绝"
                            for cid in chat_ids:
                                send_feishu_msg(hint, target_chat_id=cid)
                        pending_permission.pop(sname)
                        log.info(f"[权限] 确认超时: {sname} {tool_name} {detail[:60]}, remote={is_remote}")

                # 清理超时的 pending_image（30 秒没有 tool_result 就放弃）
                if sname in pending_image and time.time() - pending_image[sname]["time"] > 30:
                    pending_image.pop(sname)

                # ── UI 检测（plan + 选择菜单，无论有没有新 JSONL 内容都执行）──
                ui_screen = capture_pane(sname, lines=15)

                # Plan 内容检测（屏幕层降级：JSONL 的 ExitPlanMode 检测优先）
                if sname not in plan_notified:
                    if "Would you like to" in ui_screen and "plan" in ui_screen.lower():
                        # JSONL 没捕获到 plan → 从屏幕找 plan 文件路径
                        if is_remote:
                            full_screen = capture_pane(sname, lines=CAPTURE_LINES)
                            plan_path = None
                            for sline in full_screen.split("\n"):
                                m = re.search(r"(~?/.claude/plans/\S+\.md)", sline)
                                if m:
                                    plan_path = os.path.expanduser(m.group(1))
                                    break
                            if plan_path and os.path.isfile(plan_path):
                                try:
                                    with open(plan_path, "r") as pf:
                                        plan_content = pf.read()
                                    if len(plan_content) > 2000:
                                        plan_content = plan_content[:2000] + "\n\n...（已截断，完整版发 /file " + plan_path + "）"
                                    for cid in chat_ids:
                                        send_feishu_msg(f"📋 **Plan 内容：**\n\n{plan_content}", target_chat_id=cid, use_card=True)
                                except Exception as e:
                                    log.error(f"读取 plan 文件失败: {e}")
                        plan_notified.add(sname)
                        log.info(f"[Screen] 检测到 plan: {sname}, remote={is_remote}")
                elif sname in plan_notified:
                    if "Would you like to" not in ui_screen:
                        plan_notified.discard(sname)

                # 选择菜单检测（屏幕层：降级方案 + 补充 JSONL 检测）
                if "Enter to select" in ui_screen:
                    if sname not in menu_notified:
                        # JSONL 没检测到（降级方案）：从屏幕解析
                        options = parse_menu_options(ui_screen)
                        if options:
                            if is_remote:
                                msg = "📋 请选择（发数字即可）：\n"
                                for o in options:
                                    msg += f"{o['num']}. {o['text']}\n"
                                msg += "\n或直接发文字自定义回复"
                                for cid in chat_ids:
                                    send_feishu_msg(msg, target_chat_id=cid)
                            menu_notified.add(sname)
                            menu_state[sname] = options
                            log.info(f"[Screen] 检测到选择菜单: {sname}, {len(options)} 个选项, remote={is_remote}")
                    else:
                        # JSONL 已检测，用屏幕数据更新 menu_state
                        # （补充 UI 自动添加的 "Type something" 等选项，确保导航准确）
                        options = parse_menu_options(ui_screen)
                        if options and len(options) > len(menu_state.get(sname, [])):
                            menu_state[sname] = options
                            log.debug(f"[Screen] 更新菜单选项: {sname}, {len(options)} 个")
                else:
                    if sname in menu_notified:
                        menu_notified.discard(sname)
                        menu_state.pop(sname, None)

        except Exception as e:
            log.error(f"JSONL 监控异常: {e}")


# ── Feishu IM Layer: Inbound Message Handling ────────────────────

def on_message(data):
    """[IM-LAYER] Handle incoming Feishu message events (via WebSocket).

    This is the entry point for all user messages from Feishu.
    Validates sender against ALLOWED_USER_ID whitelist, deduplicates messages,
    then delegates to handle_command() for routing.
    """
    event = data.event
    if not event or not event.message:
        return

    msg = event.message
    sender = event.sender

    # 只处理白名单用户；除非显式 ALLOW_ALL_USERS=true
    sender_open_id = None
    if sender and sender.sender_id:
        sender_open_id = sender.sender_id.open_id
    if not whitelist_allows_sender(ALLOWED_USER_ID, sender_open_id, ALLOW_ALL_USERS):
        log.warning(f"非白名单或未配置白名单用户消息，忽略: {sender_open_id}")
        return

    # 只处理文本消息
    if msg.message_type != "text":
        return

    # 去重：同一条消息不处理两次
    msg_id = msg.message_id
    if msg_id in seen_message_ids:
        log.info(f"重复消息，忽略: {msg_id}")
        return
    seen_message_ids.add(msg_id)
    # 防止 set 无限增长，只保留最近 200 条
    if len(seen_message_ids) > 200:
        seen_message_ids.clear()

    # 解析消息内容
    try:
        content = json.loads(msg.content)
        text = content.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return

    if not text:
        return

    log.info(f"收到消息: {text} (chat: {msg.chat_id})")
    handle_command(text, msg.chat_id)


# ── Feishu IM Layer: Reconnect Message Recovery ──────────────────

def catchup_missed_messages():
    """[IM-LAYER] Pull missed messages via Feishu REST API after WebSocket reconnect.

    When the WebSocket connection drops and recovers, any messages sent during
    the gap are lost. This function uses the ListMessage API to fetch messages
    between last_disconnect_time and last_connect_time, then replays them
    through handle_command(). Also notifies remote-mode sessions about the gap.
    """
    global last_disconnect_time
    if not last_disconnect_time or not lark_client:
        return

    gap_seconds = last_connect_time - last_disconnect_time
    if gap_seconds < 5:
        # 断连不到 5 秒，消息丢失概率很低，跳过
        return

    log.info(f"检测到断连 {gap_seconds:.0f} 秒，开始补拉消息...")

    # 遍历所有已绑定的群聊
    total_recovered = 0
    for chat_id, session_name in chat_session_map.items():
        try:
            request = ListMessageRequest.builder() \
                .container_id_type("chat") \
                .container_id(chat_id) \
                .start_time(str(int(last_disconnect_time))) \
                .end_time(str(int(last_connect_time))) \
                .sort_type("ByCreateTimeAsc") \
                .page_size(50) \
                .build()

            response = lark_client.im.v1.message.list(request)
            if not response.success():
                log.warning(f"补拉 {session_name} 消息失败: {response.msg}")
                continue

            items = response.data.items if response.data and response.data.items else []
            recovered = 0
            for item in items:
                msg_id = item.message_id
                if msg_id in seen_message_ids:
                    continue
                # 只处理用户发的文本消息（跳过 bot 自己发的）
                if not item.sender or item.sender.sender_type != "user":
                    continue
                if item.message_type != "text":
                    continue
                try:
                    content = json.loads(item.body.content)
                    text = content.get("text", "").strip()
                except (json.JSONDecodeError, AttributeError, TypeError):
                    continue
                if not text:
                    continue

                seen_message_ids.add(msg_id)
                recovered += 1
                log.info(f"补拉到消息: {text} (chat: {chat_id})")
                handle_command(text, chat_id)

            if recovered > 0:
                log.info(f"从 {session_name} 补拉了 {recovered} 条消息")
            total_recovered += recovered

        except Exception as e:
            log.error(f"补拉 {session_name} 消息异常: {e}")

    # 通知远程模式的群聊
    for chat_id, sname in chat_session_map.items():
        if remote_mode.get(sname, False):
            msg = f"⚡ WebSocket 断连 {gap_seconds:.0f} 秒，已重连"
            if total_recovered > 0:
                msg += f"，补拉了 {total_recovered} 条消息"
            send_feishu_msg(msg, target_chat_id=chat_id)

    last_disconnect_time = 0  # 补拉完毕，重置


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
