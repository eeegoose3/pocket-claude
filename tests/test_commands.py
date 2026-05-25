import unittest

import commands
from commands import CommandContext, handle_command, parse_menu_options, find_menu_cursor


class CommandTests(unittest.TestCase):
    def make_ctx(self):
        self.messages = []
        self.sent_keys = []
        self.remote = []

        def send_feishu_msg(text, target_chat_id=None, use_card=None):
            self.messages.append({"text": text, "target": target_chat_id, "use_card": use_card})

        return CommandContext(
            app_id="app",
            app_secret="secret",
            allowed_user_id="user",
            default_agent="claude",
            claude_projects_dir="/tmp/claude",
            codex_sessions_dir="/tmp/codex",
            last_connect_time=0,
            last_disconnect_time=0,
            chat_session_map={},
            session_jsonl_id={},
            session_backend={},
            session_start_time={},
            remote_mode={},
            menu_notified=set(),
            menu_state={},
            normalize_agent=lambda agent: agent or "claude",
            get_backend=lambda name: "claude",
            backend_display=lambda name: "Claude Code",
            save_bindings=lambda: None,
            send_feishu_msg=send_feishu_msg,
            send_feishu_file=lambda *a, **kw: None,
            create_feishu_chat=lambda name: None,
            create_tmux_and_run=lambda name, cmd: (True, ""),
            load_recent_history=lambda *a, **kw: [],
            enter_remote_mode=lambda name, chats: self.remote.append(("enter", name, chats)),
            exit_remote_mode=lambda name, chats, reason="": self.remote.append(("exit", name, chats, reason)),
            ensure_remote_mode=lambda name: self.remote.append(("ensure", name)),
            send_keys=lambda name, text: self.sent_keys.append((name, text)),
            start_caffeinate=lambda: None,
            stop_caffeinate=lambda: None,
            is_caffeinate_running=lambda: False,
        )

    def test_parse_menu_options_and_cursor(self):
        screen = "  1. First\n❯ 2. Second\n  3. Third"
        self.assertEqual(
            parse_menu_options(screen),
            [
                {"num": 1, "text": "First"},
                {"num": 2, "text": "Second"},
                {"num": 3, "text": "Third"},
            ],
        )
        self.assertEqual(find_menu_cursor(screen), 2)

    def test_help_allows_missing_slash(self):
        ctx = self.make_ctx()
        handle_command("help", "chat", ctx)
        self.assertTrue(self.messages)
        self.assertIn("tmux-bridge 命令", self.messages[0]["text"])

    def test_plain_text_bound_session_sends_keys(self):
        ctx = self.make_ctx()
        ctx.chat_session_map["chat"] = "work"
        old_session_exists = commands.session_exists
        try:
            commands.session_exists = lambda name: True
            handle_command("hello", "chat", ctx)
        finally:
            commands.session_exists = old_session_exists

        self.assertEqual(self.sent_keys, [("work", "hello")])
        self.assertIn(("ensure", "work"), self.remote)
        self.assertEqual(self.messages[-1]["text"], "→ 已发送到 work")


if __name__ == "__main__":
    unittest.main()
