"""Background monitoring for Claude/Codex JSONL logs and tmux screen fallback."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
import hashlib
import logging
import os
import re
import time

from backends import find_log_by_session_id, jsonl_candidates_for_agent
from commands import parse_menu_options
from formatting import clean_ansi
from screen_classifier import classify_screen_input
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
from tmux import capture_pane, tmux_run

log = logging.getLogger("bridge")


@dataclass
class MonitorContext:
    poll_interval: float
    capture_lines: int
    bridge_sent_window: float
    chat_session_map: dict[str, str]
    session_jsonl_id: dict[str, str]
    session_backend: dict[str, str]
    session_runtime: dict[str, dict[str, Any]]
    session_start_time: dict[str, float]
    remote_mode: dict[str, bool]
    bridge_sent_time: dict[str, float]
    get_backend: Callable[[str], str]
    save_bindings: Callable[[], None]
    exit_remote_mode: Callable[[str, list[str], str], None]
    send_im_msg: Callable[..., None]
    send_im_file: Callable[..., None]


def _match_text(text: str) -> str:
    """Normalize text for screen-vs-JSONL ownership matching."""
    text = clean_ansi(text or "").lower()
    # Strip markdown, bullets, box drawing, prompts, and whitespace. Keep CJK,
    # letters, digits, and path punctuation that helps distinguish sessions.
    text = re.sub(r"[`*_#>›•│╭╮╰╯─\s]+", "", text)
    text = re.sub(r"[，。！？、：:;,.!?\[\](){}<>\"\']+", "", text)
    return text


def _jsonl_recent_snippets(fpath: str, agent: str) -> list[tuple[str, str]]:
    """Return recent user/assistant snippets from a JSONL file."""
    from collections import deque

    snippets: list[tuple[str, str]] = []
    with open(fpath, "r") as f:
        tail_lines = deque(f, maxlen=160)

    for line in tail_lines:
        user_text = extract_user_text(line, agent)
        if user_text:
            snippets.append(("user", user_text))
        assistant_text = extract_assistant_text(line, agent)
        if assistant_text:
            snippets.append(("assistant", assistant_text))
    return snippets[-12:]


def _score_jsonl_against_screen(fpath: str, agent: str, screen_text: str) -> tuple[int, int, set[str]]:
    """Score how strongly one JSONL matches the visible tmux screen.

    Returns (score, longest_match_len, matched_roles). Short generic snippets
    such as OK are intentionally weak and cannot lock ownership alone.
    """
    screen_norm = _match_text(screen_text)
    if not screen_norm:
        return 0, 0, set()

    score = 0
    longest = 0
    roles: set[str] = set()
    seen = set()
    for role, snippet in _jsonl_recent_snippets(fpath, agent):
        norm = _match_text(snippet)
        if len(norm) < 4 or norm in seen:
            continue
        seen.add(norm)
        if norm in screen_norm:
            longest = max(longest, len(norm))
            roles.add(role)
            if len(norm) >= 20:
                score += 4
            elif len(norm) >= 12:
                score += 3
            elif len(norm) >= 8:
                score += 2
            else:
                score += 1
    # A visible user+assistant pair is stronger than a single line.
    if len(roles) >= 2:
        score += 2
    return score, longest, roles


def verify_jsonl_by_screen(session_name, candidate_files, ctx):
    """Verify which JSONL belongs to a tmux session by visible content only.

    Ownership is locked only when recent user/assistant text in the JSONL can
    be found on the current tmux screen. We intentionally do not fall back to
    latest mtime: multiple Codex sessions may share the same cwd, and mtime
    fallback can wire one tmux session to another conversation.
    """
    ok, screen = tmux_run(["capture-pane", "-t", session_name, "-p"])
    if not ok or not screen or len(screen.strip()) < 10:
        return None

    agent = ctx.get_backend(session_name)
    ranked = []
    for fpath in candidate_files:
        try:
            score, longest, roles = _score_jsonl_against_screen(fpath, agent, screen)
            if score:
                ranked.append((score, longest, len(roles), fpath))
        except Exception as e:
            log.debug(f"验证 JSONL 屏幕匹配失败 {fpath}: {e}")

    if not ranked:
        log.debug("屏幕内容未匹配任何 JSONL，不锁定")
        return None

    ranked.sort(reverse=True)
    best_score, best_longest, best_roles, best_path = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0

    # Require a strong, unique content match. Examples that pass:
    # - one long assistant/user line (>=12 normalized chars)
    # - or multiple shorter user+assistant lines.
    strong_enough = best_score >= 4 and (best_longest >= 12 or best_roles >= 2)
    unique_enough = best_score > second_score
    if strong_enough and unique_enough:
        log.info(
            f"屏幕内容锁定 JSONL: {session_name} → {os.path.basename(best_path)} "
            f"(score={best_score}, longest={best_longest})"
        )
        return best_path

    log.warning(
        f"屏幕内容匹配不唯一或不够强，不锁定 JSONL: "
        f"best={best_score}, second={second_score}, longest={best_longest}"
    )
    return None




def clear_jsonl_binding(session_name, ctx, reason: str = ""):
    """Clear persisted JSONL ownership for a tmux session."""
    ctx.session_jsonl_id.pop(session_name, None)
    runtime = ctx.session_runtime.get(session_name, {})
    runtime.pop("jsonl_path", None)
    runtime.pop("jsonl_offset", None)
    runtime.pop("last_message_id", None)
    if runtime:
        ctx.session_runtime[session_name] = runtime
    else:
        ctx.session_runtime.pop(session_name, None)
    jsonl_state.pop(session_name, None)
    ctx.save_bindings()
    if reason:
        log.warning(f"已清除 JSONL 绑定: {session_name} ({reason})")


def find_jsonl_for_session(session_name, ctx):
    """找到 tmux session 中当前 CLI backend 正在写入的 JSONL 对话文件。

    Claude Code: ~/.claude/projects/<project>/*.jsonl
    Codex:       ~/.codex/sessions/YYYY/MM/DD/*.jsonl
    Generic CLI: no structured log, returns None and falls back to screen monitor.
    """
    agent = ctx.get_backend(session_name)
    if agent == "generic":
        return None

    # 已知 session_id 也必须过当前屏幕内容校验。历史错误锁定会被清掉，
    # 避免一个 tmux session 串到另一个 Codex/Claude 对话。
    known_id = ctx.session_jsonl_id.get(session_name)
    if known_id:
        match = find_log_by_session_id(known_id, agent)
        if match and os.path.exists(match):
            verified = verify_jsonl_by_screen(session_name, [match], ctx)
            if verified:
                return verified
        clear_jsonl_binding(session_name, ctx, f"未通过屏幕校验: {known_id}")

    # 获取 session 的工作目录
    ok, cwd = tmux_run(["display-message", "-t", session_name, "-p", "#{pane_current_path}"])
    if not ok or not cwd:
        return None

    jsonl_files = jsonl_candidates_for_agent(agent, cwd)
    if not jsonl_files:
        return None

    # /start 的启动时间只能说明“可能相关”，不能单独证明归属。
    # 仍然必须通过屏幕内容匹配来锁定 JSONL。

    # 排除已被其他 session 占用的 JSONL，避免同目录下多 session 互相抢文件
    claimed_ids = set(ctx.session_jsonl_id.values())
    unclaimed = [f for f in jsonl_files
                 if session_id_from_log_path(f, agent) not in claimed_ids]
    candidates = unclaimed if unclaimed else jsonl_files

    # 优先：屏幕内容交叉验证（精准匹配）
    verified = verify_jsonl_by_screen(session_name, candidates, ctx)
    if verified:
        sid = session_id_from_log_path(verified, agent)
        ctx.session_jsonl_id[session_name] = sid
        ctx.save_bindings()
        log.info(f"屏幕验证锁定 JSONL: {session_name} → {sid}")
        return verified

    # 内容匹配失败时不锁定。宁可继续 screen fallback，也不能串到别的会话。
    return None





















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


def maybe_sync_backend_from_screen(sname: str, ctx) -> str:
    """Update backend when a generic tmux session visibly enters Codex/Claude.

    This uses the same visible prompt classifier as command routing. It avoids
    process-name guesses while allowing a shell-started `codex`/`claude` to move
    from generic screen fallback to structured JSONL monitoring.
    """
    screen = clean_ansi(capture_pane(sname, lines=20))
    target = classify_screen_input(screen)
    if target.kind not in ("codex", "claude"):
        return ctx.get_backend(sname)

    current = ctx.get_backend(sname)
    if current != target.kind:
        ctx.session_backend[sname] = target.kind
        # Drop generic fallback and stale JSONL state so the next loop can lock
        # the newly visible agent by content instead of reusing old ownership.
        screen_state.pop(sname, None)
        clear_jsonl_binding(sname, ctx, f"backend 切换 {current} → {target.kind}")
        log.info(f"屏幕识别切换 backend: {sname} {current} → {target.kind}")
    return target.kind


def maybe_push_screen_update(sname, chat_ids, is_remote, ctx):
    """Generic CLI fallback: push cleaned tmux screen when it changes in remote mode."""
    screen = clean_ansi(capture_pane(sname, lines=ctx.capture_lines)).strip()
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
        ctx.send_im_msg(f"📺 {sname} 屏幕更新:\n{text}", target_chat_id=cid, use_card=False)


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


def jsonl_monitor(ctx):
    """后台监控 Claude/Codex JSONL；generic backend 降级为屏幕变化推送。"""
    global jsonl_state

    log.info("对话日志/屏幕监控已启动")
    while True:
        try:
            time.sleep(ctx.poll_interval)

            if not ctx.chat_session_map:
                continue

            # session → [chat_id, ...] 映射
            session_chats = {}
            for cid, sname in ctx.chat_session_map.items():
                session_chats.setdefault(sname, []).append(cid)

            for sname, chat_ids in session_chats.items():
                agent = maybe_sync_backend_from_screen(sname, ctx)
                is_remote = ctx.remote_mode.get(sname, False)

                # Generic CLI 没有结构化 JSONL：远程模式下用屏幕变化作为通用 fallback。
                if agent == "generic":
                    maybe_push_screen_update(sname, chat_ids, is_remote, ctx)
                    continue

                # 查找或更新 JSONL 文件路径
                state = jsonl_state.get(sname)

                if not state:
                    runtime_state = ctx.session_runtime.get(sname, {})
                    runtime_path = runtime_state.get("jsonl_path")
                    runtime_offset = runtime_state.get("jsonl_offset")
                    if runtime_path and os.path.exists(runtime_path):
                        verified_runtime = verify_jsonl_by_screen(sname, [runtime_path], ctx)
                        if verified_runtime:
                            current_size = os.path.getsize(runtime_path)
                            pos = runtime_offset if isinstance(runtime_offset, int) else current_size
                            pos = min(max(pos, 0), current_size)
                            jsonl_state[sname] = {"path": runtime_path, "pos": pos, "last_change": time.time()}
                            log.info(f"恢复 {agent} JSONL offset: {sname} → {os.path.basename(runtime_path)}:{pos}")
                            state = jsonl_state[sname]
                        else:
                            clear_jsonl_binding(sname, ctx, "持久化 JSONL 未通过屏幕内容校验")
                            state = None
                    else:
                        state = None

                if not state:
                    jsonl_path = find_jsonl_for_session(sname, ctx)
                    if not jsonl_path:
                        # Agent may be on a startup/login/model-picker screen before logs exist.
                        maybe_push_screen_update(sname, chat_ids, is_remote, ctx)
                        continue
                    # 从文件末尾开始（不发送历史消息）
                    pos = os.path.getsize(jsonl_path)
                    jsonl_state[sname] = {"path": jsonl_path, "pos": pos, "last_change": time.time()}
                    ctx.session_runtime[sname] = {"jsonl_path": jsonl_path, "jsonl_offset": pos}
                    ctx.save_bindings()
                    log.info(f"监控 {agent} JSONL: {sname} → {os.path.basename(jsonl_path)}")
                    continue

                jsonl_path = state["path"]
                pos = state["pos"]

                # 检查文件是否还存在
                if not os.path.exists(jsonl_path):
                    jsonl_state.pop(sname, None)
                    ctx.session_jsonl_id.pop(sname, None)  # 文件没了，解除锁定
                    continue

                # 读取新增内容
                current_size = os.path.getsize(jsonl_path)
                if current_size > pos:
                    with open(jsonl_path, "r") as f:
                        f.seek(pos)
                        new_content = f.read()

                    jsonl_state[sname] = {"path": jsonl_path, "pos": current_size, "last_change": time.time()}
                    ctx.session_runtime[sname] = {"jsonl_path": jsonl_path, "jsonl_offset": current_size}
                    ctx.save_bindings()

                    # 解析 JSONL 新内容：user 消息检测 + assistant 回复 + 交互式 UI + 系统事件 + 权限确认
                    for line in new_content.strip().split("\n"):
                        if not line.strip():
                            continue

                        # ⓪ user 消息检测（远程模式：检测本地键盘输入 → 自动退出）
                        user_text = extract_user_text(line, agent)
                        if user_text and is_remote:
                            if time.time() - ctx.bridge_sent_time.get(sname, 0) > ctx.bridge_sent_window:
                                # 本地键盘输入 → 推送内容 + 退出远程模式
                                for cid in chat_ids:
                                    ctx.send_im_msg(f"👤 本地输入：{user_text}", target_chat_id=cid, use_card=False)
                                ctx.exit_remote_mode(sname, chat_ids, "检测到本地键盘输入")
                                is_remote = False

                        # ① assistant 文本回复 → 推送到飞书（仅远程模式）
                        text = extract_assistant_text(line, agent)
                        if text and is_remote:
                            for cid in chat_ids:
                                ctx.send_im_msg(text, target_chat_id=cid, use_card=True)

                        # ② 交互式 UI 检测（AskUserQuestion / ExitPlanMode / 权限确认）
                        ui = extract_interactive_ui(line, agent)
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
                                            ctx.send_im_msg(msg, target_chat_id=cid)
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
                                            ctx.send_im_msg(f"📋 **Plan 内容：**\n\n{plan_text}", target_chat_id=cid, use_card=True)
                                plan_notified.add(sname)
                                log.info(f"[JSONL] 检测到 plan: {sname}, remote={is_remote}")

                            elif ui["type"] == "tool_pending":
                                # 可能需要权限确认的工具调用，记录并等 PERMISSION_WAIT 秒
                                pending_permission[sname] = {
                                    "id": ui["id"], "name": ui["name"],
                                    "detail": ui["detail"], "time": time.time(),
                                }

                        # ②b 图片写入检测 — 记录待确认的图片文件
                        img = extract_image_write(line, agent)
                        if img:
                            pending_image[sname] = {
                                "id": img["tool_id"], "path": img["path"], "time": time.time(),
                            }

                        # ③ tool_result 到达 → 清除权限等待状态 + 推送图片
                        if sname in pending_permission:
                            if check_tool_result(line, pending_permission[sname]["id"], agent):
                                pending_permission.pop(sname, None)
                        if sname in pending_image:
                            pi = pending_image[sname]
                            if check_tool_result(line, pi["id"], agent):
                                # Write 图片工具执行成功，自动推送
                                if is_remote:
                                    img_path = pi["path"]
                                    if img_path == "__screenshot__":
                                        # Playwright 截图：从 tool_result 中提取路径
                                        real_path = extract_screenshot_path(line, pi["id"], agent)
                                        if real_path:
                                            img_path = real_path
                                        else:
                                            img_path = None
                                    if img_path and os.path.isfile(img_path):
                                        log.info(f"[图片推送] {sname} → {img_path}")
                                        for cid in chat_ids:
                                            ctx.send_im_file(img_path, target_chat_id=cid)
                                pending_image.pop(sname, None)

                        # ④ 系统事件（上下文压缩、API 错误）— 仅远程模式推送
                        evt = extract_system_event(line, agent)
                        if evt and is_remote:
                            if evt["type"] == "compact":
                                tokens = evt["pre_tokens"]
                                if tokens > 0:
                                    for cid in chat_ids:
                                        ctx.send_im_msg(f"🗜️ 上下文已压缩（压缩前 {tokens:,} tokens）", target_chat_id=cid)
                            elif evt["type"] == "api_error" and evt["retry_attempt"] >= 3:
                                for cid in chat_ids:
                                    ctx.send_im_msg(
                                        f"⚠️ API 错误，第 {evt['retry_attempt']}/{evt['max_retries']} 次重试",
                                        target_chat_id=cid,
                                    )

                    # ⑤ 回复完毕通知（仅远程模式）
                    if is_remote:
                        lines = [l for l in new_content.strip().split("\n") if l.strip()]
                        if lines and is_turn_complete(lines[-1], agent):
                            for cid in chat_ids:
                                ctx.send_im_msg("✅ 已回复完毕，等待指令", target_chat_id=cid)
                else:
                    # 文件无变化，检查是否会话切换（clear context 等）
                    last_change = state.get("last_change", time.time())
                    if time.time() - last_change > STALE_THRESHOLD:
                        new_path = find_continuation_jsonl(jsonl_path)
                        if new_path:
                            new_sid = session_id_from_log_path(new_path, agent)
                            ctx.session_jsonl_id[sname] = new_sid
                            # 从新文件末尾开始（跳过已有内容）
                            new_size = os.path.getsize(new_path)
                            jsonl_state[sname] = {"path": new_path, "pos": new_size, "last_change": time.time()}
                            ctx.session_runtime[sname] = {"jsonl_path": new_path, "jsonl_offset": new_size}
                            ctx.save_bindings()
                            log.info(f"会话切换: {sname} → {os.path.basename(new_path)}")
                            if is_remote:
                                for cid in chat_ids:
                                    ctx.send_im_msg(f"⚠️ 检测到 {sname} 会话已切换（clear context），已自动跟踪新会话", target_chat_id=cid)

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
                                ctx.send_im_msg(hint, target_chat_id=cid)
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
                            full_screen = capture_pane(sname, lines=ctx.capture_lines)
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
                                        ctx.send_im_msg(f"📋 **Plan 内容：**\n\n{plan_content}", target_chat_id=cid, use_card=True)
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
                                    ctx.send_im_msg(msg, target_chat_id=cid)
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
