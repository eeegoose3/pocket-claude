"""Command routing for Phone Agent Remote."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Any
import os
import re
import time

from backends import (
    AGENT_ALIASES,
    BACKENDS,
    find_cwd_for_session_id,
    resume_command,
    start_command,
)
from formatting import clean_ansi
from screen_classifier import classify_screen_input, looks_like_shell_command
from security import (
    approval_token_ok,
    doctor_report,
    is_user_file_allowed,
    validate_session_name,
)
from tmux import (
    capture_pane,
    list_sessions,
    send_confirm,
    send_ctrl_c,
    session_exists,
    tmux_run,
)


@dataclass
class CommandContext:
    app_id: str | None
    app_secret: str | None
    allowed_user_id: str | None
    default_agent: str
    claude_projects_dir: str
    codex_sessions_dir: str
    last_connect_time: float
    last_disconnect_time: float
    chat_session_map: dict[str, str]
    session_jsonl_id: dict[str, str]
    session_backend: dict[str, str]
    session_start_time: dict[str, float]
    remote_mode: dict[str, bool]
    menu_notified: set[str]
    menu_state: dict[str, list[dict[str, Any]]]
    normalize_agent: Callable[[str | None], str]
    get_backend: Callable[[str | None], str]
    backend_display: Callable[[str | None], str]
    save_bindings: Callable[[], None]
    send_im_msg: Callable[..., None]
    send_im_file: Callable[..., None]
    create_im_chat: Callable[[str], str | None]
    create_tmux_and_run: Callable[[str, str], tuple[bool, str]]
    load_recent_history: Callable[..., list[dict[str, str]]]
    enter_remote_mode: Callable[[str, list[str]], None]
    exit_remote_mode: Callable[[str, list[str], str], None]
    ensure_remote_mode: Callable[[str], None]
    send_keys: Callable[[str, str], None]
    start_caffeinate: Callable[[], None]
    stop_caffeinate: Callable[[], None]
    is_caffeinate_running: Callable[[], bool]


def parse_menu_options(screen_text: str) -> list[dict[str, Any]]:
    """Parse numbered terminal menu options from screen text."""
    options = []
    for line in screen_text.split("\n"):
        match = re.match(r"\s*[❯>\s]*(\d+)\.\s+(.+)", line)
        if match:
            text = match.group(2).strip()
            if text:
                options.append({"num": int(match.group(1)), "text": text})
    return options


def find_menu_cursor(screen_text: str) -> int:
    """Return the option number currently pointed to by the terminal cursor."""
    for line in screen_text.split("\n"):
        if "❯" in line:
            match = re.search(r"(\d+)\.", line)
            if match:
                return int(match.group(1))
    return 1


def select_menu_option(session: str, target_num: int) -> None:
    """Navigate a terminal menu with arrow keys and press Enter."""
    screen = capture_pane(session, lines=15)
    current = find_menu_cursor(screen)
    delta = target_num - current
    key = "Down" if delta > 0 else "Up"
    for _ in range(abs(delta)):
        tmux_run(["send-keys", "-t", session, key])
        time.sleep(0.05)
    time.sleep(0.1)
    tmux_run(["send-keys", "-t", session, "Enter"])


def input_target_for_session(session_name: str) -> str:
    """Return user-facing input target label for a tmux session."""
    screen = clean_ansi(capture_pane(session_name, lines=20))
    return classify_screen_input(screen).label


def missing_tmux_message(session_name: str) -> str:
    return (
        f"当前飞书聊天绑定的 tmux session 不存在：{session_name}\n\n"
        "可用：\n"
        "/sessions 查看正在运行的 tmux session\n"
        "/bind <name或编号> 改绑定其他 tmux session\n"
        "/start ... 重新创建并启动 CLI\n"
        "/unbind 解除当前绑定"
    )


def resolve_session_selector(selector: str, sessions: list[str]) -> str | None:
    """Resolve either a session name or a 1-based list index."""
    if selector.isdigit():
        idx = int(selector)
        if 1 <= idx <= len(sessions):
            return sessions[idx - 1]
        return None
    return selector


def handle_command(text: str, msg_chat_id: str, ctx: CommandContext) -> None:
    """Handle one incoming user command/message."""
    chat_session_map = ctx.chat_session_map
    session_jsonl_id = ctx.session_jsonl_id
    session_backend = ctx.session_backend
    session_start_time = ctx.session_start_time
    remote_mode = ctx.remote_mode
    menu_notified = ctx.menu_notified
    menu_state = ctx.menu_state
    app_id = ctx.app_id
    app_secret = ctx.app_secret
    allowed_user_id = ctx.allowed_user_id
    default_agent = ctx.default_agent
    last_connect_time = ctx.last_connect_time
    last_disconnect_time = ctx.last_disconnect_time
    normalize_agent = ctx.normalize_agent
    get_backend = ctx.get_backend
    backend_display = ctx.backend_display
    save_bindings = ctx.save_bindings
    send_im_msg = ctx.send_im_msg
    send_im_file = ctx.send_im_file
    create_im_chat = ctx.create_im_chat
    create_tmux_and_run = ctx.create_tmux_and_run
    load_recent_history = ctx.load_recent_history
    enter_remote_mode = ctx.enter_remote_mode
    exit_remote_mode = ctx.exit_remote_mode
    ensure_remote_mode = ctx.ensure_remote_mode
    send_keys = ctx.send_keys
    start_caffeinate = ctx.start_caffeinate
    stop_caffeinate = ctx.stop_caffeinate
    is_caffeinate_running = ctx.is_caffeinate_running

    APP_ID = app_id
    APP_SECRET = app_secret
    ALLOWED_USER_ID = allowed_user_id
    DEFAULT_AGENT = default_agent
    CLAUDE_PROJECTS_DIR = ctx.claude_projects_dir
    CODEX_SESSIONS_DIR = ctx.codex_sessions_dir
    text = text.strip()

    # 清理群聊 @bot 前缀
    text = re.sub(r"@_user_\d+\s*", "", text).strip()

    # 飞书有时会吞掉 / 前缀，统一补上
    cmd_words = ("help", "doctor", "list", "sessions", "status", "start", "resume", "new", "bind", "switch", "unbind", "screen", "file", "y", "n", "cancel", "caffeinate", "remote", "local")
    first_word = text.split()[0] if text.split() else ""
    if first_word in cmd_words:
        text = "/" + text

    # 当前对话绑定的 session
    bound = chat_session_map.get(msg_chat_id)

    # /help
    if text == "/help":
        send_im_msg(
            "Phone Agent Remote 命令：\n\n"
            "【常用】\n"
            "/status — 查看当前飞书聊天绑定的 tmux 状态\n"
            "/sessions — 列出正在运行的 tmux session\n"
            "/bind <编号或名称> — 当前飞书聊天绑定 tmux session\n"
            "/screen — 截取当前 tmux 画面，并判断输入目标\n"
            "/start [claude|codex] <name> <目录> — 新建 tmux 并启动 CLI\n"
            "/new <name> [claude|codex|generic] — 给已有 tmux 创建飞书聊天\n"
            "/unbind — 解除当前飞书聊天和 tmux 的绑定\n\n"
            "【会话内】\n"
            "/file <路径> — 发送本地文件到飞书（需 FILE_ALLOW_DIRS）\n"
            "/y / /n — 给 CLI 交互确认发送 y/n\n"
            "/cancel — 发送 Ctrl+C\n"
            "/remote / /local — 手动切换远程/本地推送模式\n"
            "/caffeinate — 切换防睡眠\n\n"
            "其他文本会输入到当前绑定的 tmux session。"
        )
        return

    # /doctor
    if text == "/doctor":
        send_im_msg(doctor_report(APP_ID, APP_SECRET, ALLOWED_USER_ID, CLAUDE_PROJECTS_DIR, CODEX_SESSIONS_DIR), use_card=False)
        return

    # /caffeinate — 切换 Mac 防睡眠
    if text == "/caffeinate":
        if is_caffeinate_running():
            stop_caffeinate()
            send_im_msg("☕ 防睡眠已关闭，Mac 会正常休眠")
        else:
            start_caffeinate()
            send_im_msg("☕ 防睡眠已开启，Mac 不会自动休眠\n（断连期间的消息仍会在重连后自动补拉）")
        return

    # /sessions (/list 兼容)
    if text in ("/sessions", "/list"):
        sessions = list_sessions()
        if not sessions:
            send_im_msg("没有正在运行的 tmux session")
        else:
            bound_sessions = {}
            for cid, sname in chat_session_map.items():
                bound_sessions.setdefault(sname, []).append(cid)
            lines = ["tmux sessions:"]
            for idx, s in enumerate(sessions, start=1):
                marker = " ← 当前绑定" if s == bound else (" (已绑定)" if s in bound_sessions else "")
                target = input_target_for_session(s)
                lines.append(f"{idx}. {s} | 输入目标：{target}{marker}")
            lines.append("\n可用 /bind <编号或名称> 绑定当前飞书聊天")
            send_im_msg("\n".join(lines))
        return

    # /status — 当前聊天和 tmux session 状态
    if text == "/status":
        lines = ["📊 Bridge 状态"]
        if bound:
            lines.append(f"当前飞书聊天绑定 tmux：{bound}")
            if session_exists(bound):
                target = input_target_for_session(bound)
                lines.append("tmux 状态：在线")
                lines.append(f"输入目标：{target}")
                agent = get_backend(bound)
                jid = session_jsonl_id.get(bound)
                if jid:
                    lines.append(f"最近日志：{agent} {jid[:8]}")
                else:
                    lines.append(f"最近日志：{agent} 未绑定")
            else:
                lines.append("tmux 状态：不存在")
                lines.append("提示：发送 /sessions 改绑，或 /start ... 重新创建")
        else:
            lines.append("当前飞书聊天未绑定 tmux session")
            lines.append("提示：发送 /sessions 查看可绑定项，或 /start ... 新建")

        all_sessions = sorted(set(chat_session_map.values()))
        if all_sessions:
            lines.append("\n已记录绑定：")
            for sname in all_sessions:
                status = "在线" if session_exists(sname) else "不存在"
                lines.append(f"- {sname}: {status}")

        if last_connect_time > 0:
            ws_ago = int(time.time() - last_connect_time)
            if ws_ago < 60:
                ws_str = f"已连接（{ws_ago}秒前）"
            elif ws_ago < 3600:
                ws_str = f"已连接（{ws_ago // 60}分钟前）"
            else:
                ws_str = f"已连接（{ws_ago // 3600}小时前）"
        else:
            ws_str = "未连接"
        lines.append(f"\nWebSocket: {ws_str}")
        if last_disconnect_time > 0:
            dc_ago = int(time.time() - last_disconnect_time)
            if dc_ago < 60:
                lines.append(f"上次断连: {dc_ago}秒前")
            elif dc_ago < 3600:
                lines.append(f"上次断连: {dc_ago // 60}分钟前")
        caf = "开启" if is_caffeinate_running() else "关闭"
        lines.append(f"caffeinate: {caf}")
        send_im_msg("\n".join(lines))
        return

    # /start [agent] <name> <目录> — 新建 Claude Code / Codex / generic CLI
    if text.startswith("/start"):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            send_im_msg(
                "用法: /start [claude|codex] <session名> <项目目录>\n"
                "例: /start codex marketing ~/Claude_code/marketing\n"
                "兼容旧用法: /start marketing ~/Claude_code/marketing（使用 DEFAULT_AGENT）"
            )
            return
        maybe_agent = normalize_agent(parts[1])
        if maybe_agent in ("claude", "codex", "generic") and parts[1].lower() in AGENT_ALIASES:
            agent = maybe_agent
            rest = parts[2].split(maxsplit=1)
            if len(rest) < 2:
                send_im_msg("用法: /start <agent> <session名> <项目目录>")
                return
            name, directory = rest[0].strip(), rest[1].strip()
        else:
            agent = normalize_agent(DEFAULT_AGENT)
            name, directory = parts[1].strip(), parts[2].strip()
        err = validate_session_name(name)
        if err:
            send_im_msg(err)
            return
        directory = directory.replace("~", os.path.expanduser("~"))
        if not os.path.isdir(directory):
            send_im_msg(f"项目目录不存在: {directory}")
            return
        display = BACKENDS[agent]["display"]
        if session_exists(name):
            send_im_msg(
                f"tmux session '{name}' 已存在。\n"
                f"我不会往已有 tmux 里自动输入启动命令。\n"
                f"如果要控制它，发送 /bind {name}；如果要新开，请换一个 session 名。"
            )
            return
        session_backend[name] = agent
        save_bindings()
        # 记录启动时间，用于后续精确识别新创建的日志文件
        session_start_time[name] = time.time()
        cmd = start_command(agent, directory)
        ok, err = create_tmux_and_run(name, cmd)
        if not ok:
            send_im_msg(err)
            return
        send_im_msg(f"已创建 tmux session '{name}'，{display} 启动中...")
        time.sleep(3)
        # 创建飞书会话
        existing = [cid for cid, sname in chat_session_map.items() if sname == name]
        if existing:
            send_im_msg(f"'{name}' 已有飞书窗口，无需重复创建")
            return
        new_chat_id = create_im_chat(name)
        if new_chat_id:
            chat_session_map[new_chat_id] = name
            save_bindings()
            send_im_msg(f"已绑定到 {display}，去聊天列表找 '{name}'", target_chat_id=new_chat_id)
        else:
            send_im_msg(f"飞书会话创建失败，可手动发 /new {name}")
        return

    # /resume [agent] <name> <session-id> — 恢复历史对话
    if text.startswith("/resume"):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            send_im_msg(
                "用法: /resume [claude|codex] <session名> <session-id>\n"
                "例: /resume codex ease-video 019e5e21-b1a3-75c2-8521-5391b4ff644b"
            )
            return
        maybe_agent = normalize_agent(parts[1])
        if maybe_agent in ("claude", "codex", "generic") and parts[1].lower() in AGENT_ALIASES:
            agent = maybe_agent
            rest = parts[2].split(maxsplit=1)
            if len(rest) < 2:
                send_im_msg("用法: /resume <agent> <session名> <session-id>")
                return
            name = rest[0].strip()
            raw_session_id = rest[1]
        else:
            name = parts[1].strip()
            agent = session_backend.get(name, normalize_agent(DEFAULT_AGENT))
            raw_session_id = parts[2]
        err = validate_session_name(name)
        if err:
            send_im_msg(err)
            return
        # 飞书可能在长 ID 中插入换行，清理掉所有空白字符
        session_id = re.sub(r"\s+", "", raw_session_id)
        if session_exists(name):
            send_im_msg(
                f"tmux session '{name}' 已存在。\n"
                "我不会往已有 tmux 里自动输入 Codex/Claude resume 命令。\n"
                f"如果要控制它，发送 /bind {name}；如果要恢复历史，请换一个新的 tmux session 名。"
            )
            return
        # 从日志查找项目目录
        cwd = find_cwd_for_session_id(session_id, agent)
        if not cwd:
            send_im_msg(f"找不到 session-id '{session_id}' 对应的对话记录")
            return
        session_backend[name] = agent
        # 记录 session_id，用于精确锁定日志文件
        session_jsonl_id[name] = session_id
        save_bindings()
        cmd = resume_command(agent, cwd, session_id)
        display = BACKENDS[agent]["display"]
        ok, err = create_tmux_and_run(name, cmd)
        if not ok:
            send_im_msg(err)
            return
        send_im_msg(f"已创建 tmux session '{name}'，正在恢复对话...")
        # 等 CLI 启动
        time.sleep(3)
        # 创建飞书会话（检查是否已有）
        existing = [cid for cid, sname in chat_session_map.items() if sname == name]
        if existing:
            send_im_msg(f"'{name}' 已有飞书窗口，无需重复创建")
            return
        new_chat_id = create_im_chat(name)
        if new_chat_id:
            chat_session_map[new_chat_id] = name
            save_bindings()
            # 加载最近 3 轮对话历史并发送到新群聊
            history = load_recent_history(session_id, agent=agent)
            if history:
                for msg in history:
                    if msg["role"] == "user":
                        send_im_msg(f"👤 你：{msg['text']}", target_chat_id=new_chat_id, use_card=False)
                    else:
                        text = msg["text"]
                        if len(text) > 500:
                            text = text[:500] + "...（已截断）"
                        send_im_msg(f"🤖 {display}：{text}", target_chat_id=new_chat_id, use_card=True)
                send_im_msg("── 以上是历史记录 ──", target_chat_id=new_chat_id, use_card=False)
            send_im_msg(f"已绑定，去聊天列表找 '{name}'", target_chat_id=new_chat_id)
        else:
            send_im_msg(f"tmux session 已创建，但飞书会话创建失败。可手动发 /new {name}")
        return

    # /new <session> [agent] — 自动创建飞书会话并绑定
    if text.startswith("/new"):
        parts = text.split()
        if len(parts) < 2:
            send_im_msg("用法: /new <tmux-session名> [claude|codex|generic]")
            return
        name = parts[1].strip()
        err = validate_session_name(name)
        if err:
            send_im_msg(err)
            return
        if len(parts) >= 3:
            session_backend[name] = normalize_agent(parts[2])
        else:
            get_backend(name)
        if not session_exists(name):
            sessions = list_sessions()
            send_im_msg(f"tmux session '{name}' 不存在\n可用: {', '.join(sessions)}")
            return
        # 检查是否已有会话绑定到这个 session
        existing = [cid for cid, sname in chat_session_map.items() if sname == name]
        if existing:
            send_im_msg(f"tmux session '{name}' 已有绑定的飞书聊天，无需重复创建\n如需重建，先在对应聊天里发 /unbind")
            return
        send_im_msg(f"正在创建会话 '{name}'...")
        new_chat_id = create_im_chat(name)
        if not new_chat_id:
            send_im_msg("创建会话失败，请检查 im:chat 权限是否已添加并发版")
            return
        chat_session_map[new_chat_id] = name
        save_bindings()
        # 在新会话里发欢迎消息 + 截屏
        send_im_msg(
            f"已绑定到 tmux session: {name} ({backend_display(name)})\n直接在这里发消息即可控制",
            target_chat_id=new_chat_id,
        )
        screen = clean_ansi(capture_pane(name))
        if screen:
            send_im_msg(f"📺 当前屏幕:\n{screen}", target_chat_id=new_chat_id)
        send_im_msg(f"会话 '{name}' 已创建，去聊天列表找到它")
        return

    # /bind <name> 或 /switch <name>（兼容旧命令）
    if text.startswith("/bind") or text.startswith("/switch"):
        parts = text.split()
        if len(parts) < 2:
            send_im_msg("用法: /bind <session名> [claude|codex|generic]")
            return
        sessions = list_sessions()
        name = resolve_session_selector(parts[1].strip(), sessions)
        if not name:
            send_im_msg(f"编号无效。可用: {', '.join(sessions) if sessions else '无'}")
            return
        err = validate_session_name(name)
        if err:
            send_im_msg(err)
            return
        if not session_exists(name):
            send_im_msg(f"tmux session '{name}' 不存在\n可用: {', '.join(sessions)}")
            return
        if len(parts) >= 3:
            session_backend[name] = normalize_agent(parts[2])
        else:
            get_backend(name)
        chat_session_map[msg_chat_id] = name
        save_bindings()
        send_im_msg(f"已绑定到 tmux session: {name} ({backend_display(name)})")
        # 绑定后立即截屏
        screen = clean_ansi(capture_pane(name))
        if screen:
            send_im_msg(f"📺 {name} 当前屏幕:\n{screen}")
        return

    # /unbind
    if text == "/unbind":
        if msg_chat_id in chat_session_map:
            old = chat_session_map.pop(msg_chat_id)
            save_bindings()
            send_im_msg(f"已解绑 tmux session: {old}")
        else:
            send_im_msg("当前飞书聊天未绑定任何 tmux session")
        return

    # ── 以下命令需要已绑定 session ──

    if not bound:
        send_im_msg("当前飞书聊天还没有绑定 tmux session。\n发送 /sessions 查看可绑定项，或 /start ... 新建。")
        return
    if not session_exists(bound):
        send_im_msg(missing_tmux_message(bound))
        return

    # /screen
    if text == "/screen":
        screen = clean_ansi(capture_pane(bound))
        if screen:
            target = classify_screen_input(screen).label
            send_im_msg(f"📺 {bound}:\n{screen}\n\n输入目标：{target}")
        else:
            send_im_msg("屏幕为空")
        return

    # /file <路径> — 发送本地文件到飞书
    if text.startswith("/file"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_im_msg("用法: /file <文件路径>\n例: /file ~/Documents/report.pdf")
            return
        ok, path_or_msg = is_user_file_allowed(parts[1].strip())
        if not ok:
            send_im_msg(f"⚠️ {path_or_msg}")
            return
        send_im_file(path_or_msg)
        return

    # /remote — 手动进入远程模式
    if text == "/remote":
        if remote_mode.get(bound, False):
            send_im_msg("📱 已在远程模式中")
        else:
            all_chats = [cid for cid, sn in chat_session_map.items() if sn == bound]
            enter_remote_mode(bound, all_chats)
        return

    # /local — 手动切换到本地模式
    if text == "/local":
        if not remote_mode.get(bound, False):
            send_im_msg("💻 已在本地模式中")
        else:
            all_chats = [cid for cid, sn in chat_session_map.items() if sn == bound]
            exit_remote_mode(bound, all_chats, "手动切换")
        return

    # /y /n 快捷确认
    if text.startswith("/y") or text.startswith("/n"):
        y_parts = text.split()
        if y_parts[0] not in ("/y", "/n"):
            # Avoid treating arbitrary /yes-like text as approval.
            pass
        elif not approval_token_ok(y_parts):
            send_im_msg("🔐 当前已启用 APPROVAL_TOKEN，请使用 `/y <token>` 或 `/n <token>`")
            return
        elif y_parts[0] in ("/y", "/n"):
            ensure_remote_mode(bound)
            answer = y_parts[0][1]
            send_confirm(bound, answer)
            send_im_msg(f"已发送: {answer}")
            return

    # /cancel
    if text == "/cancel":
        ensure_remote_mode(bound)
        send_ctrl_c(bound)
        send_im_msg("已发送 Ctrl+C")
        return

    # 选择菜单模式 — 用方向键导航而非直接输入文本
    if bound in menu_state:
        # 先确认菜单仍在屏幕上
        verify_screen = capture_pane(bound, lines=10)
        if "Enter to select" not in verify_screen:
            # 菜单已消失，清理状态，按普通消息处理
            menu_notified.discard(bound)
            menu_state.pop(bound, None)
        else:
            ensure_remote_mode(bound)
            options = menu_state[bound]
            if text.isdigit():
                target = int(text)
                valid_nums = [o["num"] for o in options]
                if target in valid_nums:
                    select_menu_option(bound, target)
                    opt_text = next(o["text"] for o in options if o["num"] == target)
                    send_im_msg(f"→ 已选择: {target}. {opt_text}")
                    menu_notified.discard(bound)
                    menu_state.pop(bound, None)
                else:
                    send_im_msg(f"⚠️ 有效选项: {', '.join(str(n) for n in valid_nums)}")
                return
            # 文字回复 — 找 "Type something" 或 "Other" 选项
            type_opt = next(
                (o for o in options if "type" in o["text"].lower() and "something" in o["text"].lower()),
                None,
            )
            if not type_opt:
                type_opt = next((o for o in options if "other" in o["text"].lower()), None)
            if type_opt:
                select_menu_option(bound, type_opt["num"])
                time.sleep(0.5)  # 等 UI 切换到文字输入
                send_keys(bound, text)
                send_im_msg(f"→ 已输入自定义回复")
            else:
                # 没有自定义输入选项，按 Esc 退出菜单再发
                tmux_run(["send-keys", "-t", bound, "Escape"])
                time.sleep(0.3)
                send_keys(bound, text)
                send_im_msg(f"→ 已退出菜单并发送")
            menu_notified.discard(bound)
            menu_state.pop(bound, None)
            return

    # 普通文本 → send-keys（自动进入远程模式）
    screen = clean_ansi(capture_pane(bound, lines=20))
    target = classify_screen_input(screen)
    if target.kind == "shell" and not looks_like_shell_command(text):
        send_im_msg(
            "当前 tmux 看起来停在 Shell。\n"
            "这句话不像 shell 命令，所以我没有发送。\n"
            "如果你想启动 Codex，可以直接发送：codex\n"
            "如果你想启动 Claude Code，可以直接发送：claude\n"
            "也可以先发送 /screen 查看当前画面。"
        )
        return
    ensure_remote_mode(bound)
    send_keys(bound, text)
    if target.kind == "shell":
        send_im_msg(f"→ 已输入到 Shell（tmux: {bound}）")
    elif target.kind in ("codex", "claude"):
        send_im_msg(f"→ 已发送到 {target.label}（tmux: {bound}）")
    else:
        send_im_msg(f"→ 已输入到 tmux: {bound}（输入目标：{target.label}）")
