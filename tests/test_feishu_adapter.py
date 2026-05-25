import json
import types
import unittest

from feishu_adapter import FeishuContext, on_message, send_message


class FeishuAdapterTests(unittest.TestCase):
    def make_ctx(self):
        self.handled = []
        self.reset = False
        return FeishuContext(
            lark_client=None,
            default_chat_id="chat",
            max_msg_len=4000,
            allowed_user_id="user_1",
            allow_all_users=False,
            seen_message_ids=set(),
            chat_session_map={},
            remote_mode={},
            last_disconnect_time=0,
            last_connect_time=0,
            handle_command=lambda text, chat_id: self.handled.append((text, chat_id)),
            reset_disconnect_time=lambda: setattr(self, "reset", True),
        )

    def event(self, *, user="user_1", message_id="m1", text="hello", msg_type="text"):
        return types.SimpleNamespace(
            event=types.SimpleNamespace(
                sender=types.SimpleNamespace(sender_id=types.SimpleNamespace(open_id=user)),
                message=types.SimpleNamespace(
                    message_type=msg_type,
                    message_id=message_id,
                    content=json.dumps({"text": text}),
                    chat_id="chat_1",
                ),
            )
        )

    def test_on_message_dispatches_whitelisted_text(self):
        ctx = self.make_ctx()
        on_message(self.event(text="/help"), ctx)
        self.assertEqual(self.handled, [("/help", "chat_1")])
        self.assertIn("m1", ctx.seen_message_ids)

    def test_on_message_ignores_duplicate_and_non_whitelist(self):
        ctx = self.make_ctx()
        on_message(self.event(message_id="m1"), ctx)
        on_message(self.event(message_id="m1", text="again"), ctx)
        on_message(self.event(user="bad", message_id="m2", text="bad"), ctx)
        self.assertEqual(self.handled, [("hello", "chat_1")])

    def test_send_message_without_client_is_safe(self):
        ctx = self.make_ctx()
        send_message("hello", ctx)


if __name__ == "__main__":
    unittest.main()
