import json
import os
import tempfile
import time
import unittest

import monitor
from monitor import MonitorContext


class MonitorTests(unittest.TestCase):
    def make_ctx(self):
        self.messages = []
        return MonitorContext(
            poll_interval=0.01,
            capture_lines=50,
            bridge_sent_window=15,
            chat_session_map={},
            session_jsonl_id={},
            session_runtime={},
            session_start_time={},
            remote_mode={},
            bridge_sent_time={},
            get_backend=lambda name: "codex",
            save_bindings=lambda: None,
            exit_remote_mode=lambda name, chats, reason="": None,
            send_feishu_msg=lambda text, **kw: self.messages.append((text, kw)),
            send_feishu_file=lambda path, **kw: None,
        )

    def test_verify_jsonl_by_screen_matches_recent_assistant_text(self):
        ctx = self.make_ctx()
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f:
            msg = {
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "hello from assistant with enough words"},
            }
            f.write(json.dumps(msg) + "\n")
            f.flush()
            old_tmux_run = monitor.tmux_run
            try:
                monitor.tmux_run = lambda args: (True, "screen shows hello from assistant with enough words now")
                self.assertEqual(monitor.verify_jsonl_by_screen("s", [f.name], ctx), f.name)
            finally:
                monitor.tmux_run = old_tmux_run

    def test_find_continuation_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            current = os.path.join(d, "old-session.jsonl")
            new = os.path.join(d, "new-session.jsonl")
            with open(current, "w") as f:
                f.write("{}\n")
            time.sleep(0.01)
            with open(new, "w") as f:
                f.write('{"sessionId":"old-session"}\n')
            self.assertEqual(monitor.find_continuation_jsonl(current), new)

    def test_maybe_push_screen_update_only_on_change_and_remote(self):
        ctx = self.make_ctx()
        monitor.screen_state.clear()
        old_capture = monitor.capture_pane
        try:
            monitor.capture_pane = lambda name, lines=50: "hello screen"
            monitor.maybe_push_screen_update("s", ["chat"], True, ctx)
            monitor.maybe_push_screen_update("s", ["chat"], True, ctx)
        finally:
            monitor.capture_pane = old_capture
            monitor.screen_state.clear()
        self.assertEqual(len(self.messages), 1)
        self.assertIn("hello screen", self.messages[0][0])


if __name__ == "__main__":
    unittest.main()
