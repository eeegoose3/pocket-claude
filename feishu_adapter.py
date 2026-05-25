"""Feishu/Lark IM adapter for tmux-bridge."""

from __future__ import annotations

import json
import logging
import os

from formatting import convert_tables_in_text, has_markdown
from im_adapter import IMContext
from security import whitelist_allows_sender

try:
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
        CreateChatRequest,
        CreateChatRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        CreateFileRequest,
        CreateFileRequestBody,
        ListMessageRequest,
    )
except ModuleNotFoundError:  # Allow pure unit tests without Feishu SDK installed.
    CreateMessageRequest = None
    CreateMessageRequestBody = None
    CreateChatRequest = None
    CreateChatRequestBody = None
    CreateImageRequest = None
    CreateImageRequestBody = None
    CreateFileRequest = None
    CreateFileRequestBody = None
    ListMessageRequest = None

log = logging.getLogger("bridge")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
FILE_TYPE_MAP = {
    ".pdf": "pdf",
    ".mp4": "mp4",
    ".mp3": "mp3",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
    ".doc": "doc",
}


FeishuContext = IMContext  # Backward-compatible provider-specific alias.


def send_message(text: str, ctx: FeishuContext, target_chat_id: str | None = None, use_card: bool | None = None) -> None:
    """Send a text or card message to Feishu."""
    cid = target_chat_id or ctx.default_chat_id
    if not cid or not ctx.client:
        log.warning("chat_id 或 IM client 未初始化，无法发送消息")
        return

    if use_card is None:
        use_card = has_markdown(text)

    chunks = []
    while len(text) > ctx.max_msg_len:
        split_pos = text.rfind("\n", 0, ctx.max_msg_len)
        if split_pos == -1:
            split_pos = ctx.max_msg_len
        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip("\n")
    if text:
        chunks.append(text)

    for chunk in chunks:
        if use_card:
            msg_type = "interactive"
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
            resp = ctx.client.im.v1.message.create(req)
            if not resp.success():
                log.error(f"发送消息失败: {resp.code} {resp.msg}")
        except Exception as e:
            log.error(f"发送消息异常: {e}")


def send_file(file_path: str, ctx: FeishuContext, target_chat_id: str | None = None) -> None:
    """Upload a local file and send it to Feishu."""
    cid = target_chat_id or ctx.default_chat_id
    if not cid or not ctx.client:
        log.warning("chat_id 或 IM client 未初始化，无法发送文件")
        return

    file_path = os.path.expanduser(file_path)
    if not os.path.isfile(file_path):
        send_message(f"文件不存在: {file_path}", ctx, target_chat_id=cid)
        return

    ext = os.path.splitext(file_path)[1].lower()
    file_name = os.path.basename(file_path)

    try:
        if ext in IMAGE_EXTS:
            with open(file_path, "rb") as f:
                body = CreateImageRequestBody.builder() \
                    .image_type("message") \
                    .image(f) \
                    .build()
                req = CreateImageRequest.builder() \
                    .request_body(body) \
                    .build()
                resp = ctx.client.im.v1.image.create(req)

            if not resp.success():
                send_message(f"图片上传失败: {resp.code} {resp.msg}", ctx, target_chat_id=cid)
                return

            content = json.dumps({"image_key": resp.data.image_key})
            msg_body = CreateMessageRequestBody.builder() \
                .msg_type("image") \
                .receive_id(cid) \
                .content(content) \
                .build()
        else:
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
                resp = ctx.client.im.v1.file.create(req)

            if not resp.success():
                send_message(f"文件上传失败: {resp.code} {resp.msg}", ctx, target_chat_id=cid)
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
        msg_resp = ctx.client.im.v1.message.create(msg_req)
        if msg_resp.success():
            send_message(f"✅ {file_name}", ctx, target_chat_id=cid)
        else:
            log.error(f"发送文件消息失败: {msg_resp.code} {msg_resp.msg}")
    except Exception as e:
        log.error(f"文件发送异常: {e}")
        send_message(f"文件发送失败: {e}", ctx, target_chat_id=cid)


def create_chat(name: str, ctx: FeishuContext) -> str | None:
    """Create a Feishu group chat and add the configured user."""
    if not ctx.client or not ctx.allowed_user_id:
        return None
    body = CreateChatRequestBody.builder() \
        .name(name) \
        .user_id_list([ctx.allowed_user_id]) \
        .build()
    req = CreateChatRequest.builder() \
        .user_id_type("open_id") \
        .set_bot_manager(True) \
        .request_body(body) \
        .build()
    try:
        resp = ctx.client.im.v1.chat.create(req)
        if resp.success():
            log.info(f"创建群聊成功: {name} -> {resp.data.chat_id}")
            return resp.data.chat_id
        log.error(f"创建群聊失败: {resp.code} {resp.msg}")
        return None
    except Exception as e:
        log.error(f"创建群聊异常: {e}")
        return None


def on_message(data, ctx: FeishuContext) -> None:
    """Handle incoming Feishu message events."""
    event = data.event
    if not event or not event.message:
        return

    msg = event.message
    sender = event.sender

    sender_open_id = None
    if sender and sender.sender_id:
        sender_open_id = sender.sender_id.open_id
    if not whitelist_allows_sender(ctx.allowed_user_id, sender_open_id, ctx.allow_all_users):
        log.warning(f"非白名单或未配置白名单用户消息，忽略: {sender_open_id}")
        return

    if msg.message_type != "text":
        return

    msg_id = msg.message_id
    if msg_id in ctx.seen_message_ids:
        log.info(f"重复消息，忽略: {msg_id}")
        return
    ctx.seen_message_ids.add(msg_id)
    if len(ctx.seen_message_ids) > 200:
        ctx.seen_message_ids.clear()

    try:
        content = json.loads(msg.content)
        text = content.get("text", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return

    if not text:
        return

    log.info(f"收到消息: {text} (chat: {msg.chat_id})")
    ctx.handle_command(text, msg.chat_id)


def catchup_missed_messages(ctx: FeishuContext) -> None:
    """Pull missed messages via Feishu REST API after WebSocket reconnect."""
    if not ctx.last_disconnect_time or not ctx.client:
        return

    gap_seconds = ctx.last_connect_time - ctx.last_disconnect_time
    if gap_seconds < 5:
        return

    log.info(f"检测到断连 {gap_seconds:.0f} 秒，开始补拉消息...")

    total_recovered = 0
    for chat_id, session_name in ctx.chat_session_map.items():
        try:
            request = ListMessageRequest.builder() \
                .container_id_type("chat") \
                .container_id(chat_id) \
                .start_time(str(int(ctx.last_disconnect_time))) \
                .end_time(str(int(ctx.last_connect_time))) \
                .sort_type("ByCreateTimeAsc") \
                .page_size(50) \
                .build()

            response = ctx.client.im.v1.message.list(request)
            if not response.success():
                log.warning(f"补拉 {session_name} 消息失败: {response.msg}")
                continue

            items = response.data.items if response.data and response.data.items else []
            recovered = 0
            for item in items:
                msg_id = item.message_id
                if msg_id in ctx.seen_message_ids:
                    continue
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

                ctx.seen_message_ids.add(msg_id)
                recovered += 1
                log.info(f"补拉到消息: {text} (chat: {chat_id})")
                ctx.handle_command(text, chat_id)

            if recovered > 0:
                log.info(f"从 {session_name} 补拉了 {recovered} 条消息")
            total_recovered += recovered

        except Exception as e:
            log.error(f"补拉 {session_name} 消息异常: {e}")

    for chat_id, sname in ctx.chat_session_map.items():
        if ctx.remote_mode.get(sname, False):
            msg = f"⚡ WebSocket 断连 {gap_seconds:.0f} 秒，已重连"
            if total_recovered > 0:
                msg += f"，补拉了 {total_recovered} 条消息"
            send_message(msg, ctx, target_chat_id=chat_id)

    ctx.reset_disconnect_time()


class FeishuAdapter:
    """Feishu/Lark implementation of the provider-neutral IMAdapter contract."""

    name = "feishu"

    def send_message(self, text: str, ctx: IMContext, target_chat_id: str | None = None, use_card: bool | None = None) -> None:
        send_message(text, ctx, target_chat_id=target_chat_id, use_card=use_card)

    def send_file(self, file_path: str, ctx: IMContext, target_chat_id: str | None = None) -> None:
        send_file(file_path, ctx, target_chat_id=target_chat_id)

    def create_chat(self, name: str, ctx: IMContext) -> str | None:
        return create_chat(name, ctx)

    def on_message(self, data: object, ctx: IMContext) -> None:
        on_message(data, ctx)

    def catchup_missed_messages(self, ctx: IMContext) -> None:
        catchup_missed_messages(ctx)
