"""Provider-neutral IM adapter contracts.

The bridge core should only depend on this small interface. Concrete IM
providers (Feishu/Lark today) translate provider SDK events and APIs into these
methods.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(init=False)
class IMContext:
    """Runtime state shared with an IM adapter.

    `client` is provider-specific: for Feishu it is a Lark SDK client. The
    remaining fields are provider-neutral bridge state.
    """

    client: object | None
    default_chat_id: str | None
    max_msg_len: int
    allowed_user_id: str | None
    allow_all_users: bool
    seen_message_ids: set[str]
    chat_session_map: dict[str, str]
    remote_mode: dict[str, bool]
    last_disconnect_time: float
    last_connect_time: float
    handle_command: Callable[[str, str], None]
    reset_disconnect_time: Callable[[], None]

    def __init__(
        self,
        client: object | None = None,
        *,
        lark_client: object | None = None,
        default_chat_id: str | None,
        max_msg_len: int,
        allowed_user_id: str | None,
        allow_all_users: bool,
        seen_message_ids: set[str],
        chat_session_map: dict[str, str],
        remote_mode: dict[str, bool],
        last_disconnect_time: float,
        last_connect_time: float,
        handle_command: Callable[[str, str], None],
        reset_disconnect_time: Callable[[], None],
    ) -> None:
        self.client = client if client is not None else lark_client
        self.default_chat_id = default_chat_id
        self.max_msg_len = max_msg_len
        self.allowed_user_id = allowed_user_id
        self.allow_all_users = allow_all_users
        self.seen_message_ids = seen_message_ids
        self.chat_session_map = chat_session_map
        self.remote_mode = remote_mode
        self.last_disconnect_time = last_disconnect_time
        self.last_connect_time = last_connect_time
        self.handle_command = handle_command
        self.reset_disconnect_time = reset_disconnect_time

    @property
    def lark_client(self) -> object | None:
        """Backward-compatible alias for older Feishu tests/callers."""
        return self.client

    @lark_client.setter
    def lark_client(self, value: object | None) -> None:
        self.client = value


class IMAdapter(Protocol):
    """Minimal interface required by the bridge core."""

    name: str

    def send_message(
        self,
        text: str,
        ctx: IMContext,
        target_chat_id: str | None = None,
        use_card: bool | None = None,
    ) -> None:
        """Send a text/card message to a chat."""
        ...

    def send_file(self, file_path: str, ctx: IMContext, target_chat_id: str | None = None) -> None:
        """Upload/send a local file to a chat."""
        ...

    def create_chat(self, name: str, ctx: IMContext) -> str | None:
        """Create a new chat for one tmux session and return its chat id."""
        ...

    def on_message(self, data: object, ctx: IMContext) -> None:
        """Handle one provider-specific incoming message event."""
        ...

    def catchup_missed_messages(self, ctx: IMContext) -> None:
        """Recover missed messages after reconnect, when the provider supports it."""
        ...
