import unittest

import session_runtime as sr
from session_runtime import SessionRuntimeContext


class SessionRuntimeTests(unittest.TestCase):
    def make_ctx(self):
        return SessionRuntimeContext(
            default_agent="claude",
            session_backend={},
            bridge_sent_time={},
        )

    def test_get_backend_uses_cached_value(self):
        ctx = self.make_ctx()
        ctx.session_backend["s"] = "openai"
        self.assertEqual(sr.get_backend("s", ctx), "codex")

    def test_get_backend_infers_from_tmux_command(self):
        ctx = self.make_ctx()
        old = sr.tmux_run
        try:
            sr.tmux_run = lambda args: (True, "codex")
            self.assertEqual(sr.get_backend("s", ctx), "codex")
            self.assertEqual(ctx.session_backend["s"], "codex")
        finally:
            sr.tmux_run = old

    def test_send_keys_records_time_and_delegates(self):
        ctx = self.make_ctx()
        calls = []
        old = sr.tmux_send_keys
        try:
            sr.tmux_send_keys = lambda session, text: calls.append((session, text))
            sr.send_keys("s", "hello", ctx)
        finally:
            sr.tmux_send_keys = old
        self.assertEqual(calls, [("s", "hello")])
        self.assertIn("s", ctx.bridge_sent_time)

    def test_create_tmux_and_run(self):
        calls = []
        old = sr.tmux_run
        try:
            sr.tmux_run = lambda args: (calls.append(args) or (True, ""))
            ok, err = sr.create_tmux_and_run("s", "cmd")
        finally:
            sr.tmux_run = old
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(calls[0], ["new-session", "-d", "-s", "s"])


if __name__ == "__main__":
    unittest.main()
