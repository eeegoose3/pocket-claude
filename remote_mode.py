"""Remote/local mode state and notifications."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging

log = logging.getLogger("bridge")

remote_mode: dict[str, bool] = {}  # {session_name: True when phone/IM controls session}


@dataclass
class RemoteModeContext:
    chat_session_map: dict[str, str]
    session_jsonl_id: dict[str, str]
    backend_display: Callable[[str | None], str]
    get_backend: Callable[[str], str]
    load_recent_history: Callable[..., list[dict[str, str]]]
    send_feishu_msg: Callable[..., None]


def enter_remote_mode(sname: str, chat_ids: list[str], ctx: RemoteModeContext) -> None:
    """Enter remote mode and push recent conversation context when available."""
    remote_mode[sname] = True
    log.info(f"[远程模式] {sname} 进入远程模式")
    display = ctx.backend_display(sname)
    sid = ctx.session_jsonl_id.get(sname)
    if sid:
        history = ctx.load_recent_history(sid, agent=ctx.get_backend(sname))
        if history:
            for cid in chat_ids:
                ctx.send_feishu_msg("── 📱 进入远程模式，以下是最近对话 ──", target_chat_id=cid, use_card=False)
            for msg in history:
                if msg["role"] == "user":
                    for cid in chat_ids:
                        ctx.send_feishu_msg(f"👤 你：{msg['text']}", target_chat_id=cid, use_card=False)
                else:
                    text = msg["text"]
                    if len(text) > 500:
                        text = text[:500] + "...（已截断）"
                    for cid in chat_ids:
                        ctx.send_feishu_msg(f"🤖 {display}：{text}", target_chat_id=cid, use_card=True)
            for cid in chat_ids:
                ctx.send_feishu_msg("── 以上是历史，以下是实时 ──", target_chat_id=cid, use_card=False)
            return
    for cid in chat_ids:
        ctx.send_feishu_msg("📱 已进入远程模式", target_chat_id=cid)


def exit_remote_mode(sname: str, chat_ids: list[str], ctx: RemoteModeContext, reason: str = "") -> None:
    """Exit remote mode and notify bound chats."""
    remote_mode[sname] = False
    log.info(f"[远程模式] {sname} 退出远程模式: {reason}")
    msg = "💻 已切换到本地模式"
    if reason:
        msg += f"（{reason}）"
    for cid in chat_ids:
        ctx.send_feishu_msg(msg, target_chat_id=cid)


def ensure_remote_mode(sname: str, ctx: RemoteModeContext) -> None:
    """Enter remote mode if the session is currently local."""
    if not remote_mode.get(sname, False):
        chat_ids = [cid for cid, sn in ctx.chat_session_map.items() if sn == sname]
        enter_remote_mode(sname, chat_ids, ctx)
