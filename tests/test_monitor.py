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
            session_backend={},
            session_runtime={},
            session_start_time={},
            remote_mode={},
            bridge_sent_time={},
            get_backend=lambda name: "codex",
            save_bindings=lambda: None,
            exit_remote_mode=lambda name, chats, reason="": None,
            send_im_msg=lambda text, **kw: self.messages.append((text, kw)),
            send_im_file=lambda path, **kw: None,
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

    def test_verify_jsonl_by_screen_prefers_content_match_over_other_same_cwd_logs(self):
        ctx = self.make_ctx()
        with tempfile.TemporaryDirectory() as d:
            wrong = os.path.join(d, "rollout-2026-05-25T10-00-00-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl")
            correct = os.path.join(d, "rollout-2026-05-25T10-01-00-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl")
            with open(wrong, "w") as f:
                f.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "这是另一个窗口里的问题"},
                }) + "\n")
                f.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "这是另一个窗口里的回答"},
                }) + "\n")
            with open(correct, "w") as f:
                f.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "你报下现在的日期"},
                }) + "\n")
                f.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "现在日期是 2026-05-25。"},
                }) + "\n")

            old_tmux_run = monitor.tmux_run
            try:
                monitor.tmux_run = lambda args: (
                    True,
                    "› 你报下现在的日期\n\n• 现在日期是 2026-05-25。\n\ngpt-5.5 high · ~/funny",
                )
                self.assertEqual(monitor.verify_jsonl_by_screen("s", [wrong, correct], ctx), correct)
            finally:
                monitor.tmux_run = old_tmux_run

    def test_verify_jsonl_by_screen_does_not_lock_on_short_ambiguous_content(self):
        ctx = self.make_ctx()
        with tempfile.TemporaryDirectory() as d:
            one = os.path.join(d, "one.jsonl")
            two = os.path.join(d, "two.jsonl")
            for path in (one, two):
                with open(path, "w") as f:
                    f.write(json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "agent_message", "message": "OK"},
                    }) + "\n")

            old_tmux_run = monitor.tmux_run
            try:
                monitor.tmux_run = lambda args: (True, "› 回复OK\n\n• OK")
                self.assertIsNone(monitor.verify_jsonl_by_screen("s", [one, two], ctx))
            finally:
                monitor.tmux_run = old_tmux_run

    def test_find_jsonl_clears_stale_known_id_and_locks_by_content(self):
        ctx = self.make_ctx()
        saved = []
        ctx.session_jsonl_id["s"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        ctx.session_runtime["s"] = {"jsonl_path": "/stale.jsonl", "jsonl_offset": 123}
        ctx.save_bindings = lambda: saved.append((dict(ctx.session_jsonl_id), dict(ctx.session_runtime)))
        with tempfile.TemporaryDirectory() as d:
            wrong = os.path.join(d, "rollout-2026-05-25T10-00-00-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl")
            correct = os.path.join(d, "rollout-2026-05-25T10-01-00-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl")
            with open(wrong, "w") as f:
                f.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "错误窗口里的回答足够长"},
                }) + "\n")
            with open(correct, "w") as f:
                f.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "你现在的路径在哪里，重复一遍"},
                }) + "\n")
                f.write(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "当前路径：/Users/chouduck/funny"},
                }) + "\n")

            old_find = monitor.find_log_by_session_id
            old_candidates = monitor.jsonl_candidates_for_agent
            old_tmux_run = monitor.tmux_run
            try:
                monitor.find_log_by_session_id = lambda sid, agent: wrong
                monitor.jsonl_candidates_for_agent = lambda agent, cwd: [wrong, correct]

                def fake_tmux_run(args):
                    if args and args[0] == "capture-pane":
                        return True, "› 你现在的路径在哪里，重复一遍\n\n• 当前路径：/Users/chouduck/funny"
                    if args and args[0] == "display-message":
                        return True, "/Users/chouduck/funny"
                    return False, ""

                monitor.tmux_run = fake_tmux_run
                self.assertEqual(monitor.find_jsonl_for_session("s", ctx), correct)
            finally:
                monitor.find_log_by_session_id = old_find
                monitor.jsonl_candidates_for_agent = old_candidates
                monitor.tmux_run = old_tmux_run

        self.assertEqual(ctx.session_jsonl_id["s"], "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        self.assertTrue(saved)

    def test_find_jsonl_does_not_fallback_to_latest_without_content_match(self):
        ctx = self.make_ctx()
        with tempfile.TemporaryDirectory() as d:
            old_path = os.path.join(d, "rollout-2026-05-25T10-00-00-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl")
            latest_path = os.path.join(d, "rollout-2026-05-25T10-01-00-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl")
            for path, msg in ((old_path, "旧文件内容足够长"), (latest_path, "最新文件内容足够长")):
                with open(path, "w") as f:
                    f.write(json.dumps({
                        "type": "event_msg",
                        "payload": {"type": "agent_message", "message": msg},
                    }) + "\n")
            os.utime(latest_path, (time.time() + 10, time.time() + 10))

            old_candidates = monitor.jsonl_candidates_for_agent
            old_tmux_run = monitor.tmux_run
            try:
                monitor.jsonl_candidates_for_agent = lambda agent, cwd: [old_path, latest_path]

                def fake_tmux_run(args):
                    if args and args[0] == "capture-pane":
                        return True, "完全不同的屏幕内容"
                    if args and args[0] == "display-message":
                        return True, "/Users/chouduck/funny"
                    return False, ""

                monitor.tmux_run = fake_tmux_run
                self.assertIsNone(monitor.find_jsonl_for_session("s", ctx))
            finally:
                monitor.jsonl_candidates_for_agent = old_candidates
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

    def test_syncs_generic_backend_to_codex_when_screen_matches(self):
        ctx = self.make_ctx()
        ctx.session_backend["s"] = "generic"
        ctx.get_backend = lambda name: ctx.session_backend.get(name, "generic")
        saved = []
        ctx.save_bindings = lambda: saved.append(dict(ctx.session_backend))
        old_capture = monitor.capture_pane
        try:
            monitor.capture_pane = lambda name, lines=20: ">_ OpenAI Codex\n\n› hello\n\ngpt-5.5 high · ~/funny"
            agent = monitor.maybe_sync_backend_from_screen("s", ctx)
        finally:
            monitor.capture_pane = old_capture

        self.assertEqual(agent, "codex")
        self.assertEqual(ctx.session_backend["s"], "codex")
        self.assertEqual(saved[-1], {"s": "codex"})

    def test_keeps_generic_backend_for_shell_screen(self):
        ctx = self.make_ctx()
        ctx.session_backend["s"] = "generic"
        ctx.get_backend = lambda name: ctx.session_backend.get(name, "generic")
        old_capture = monitor.capture_pane
        try:
            monitor.capture_pane = lambda name, lines=20: "chouduck@MacBook-Air funny %"
            agent = monitor.maybe_sync_backend_from_screen("s", ctx)
        finally:
            monitor.capture_pane = old_capture

        self.assertEqual(agent, "generic")
        self.assertEqual(ctx.session_backend["s"], "generic")


if __name__ == "__main__":
    unittest.main()
