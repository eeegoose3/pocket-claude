import unittest

import remote_mode as rm
from remote_mode import RemoteModeContext, enter_remote_mode, ensure_remote_mode, exit_remote_mode


class RemoteModeTests(unittest.TestCase):
    def setUp(self):
        rm.remote_mode.clear()
        self.messages = []

    def make_ctx(self, history=None):
        return RemoteModeContext(
            chat_session_map={"chat": "s"},
            session_jsonl_id={"s": "sid"} if history is not None else {},
            backend_display=lambda name: "Codex",
            get_backend=lambda name: "codex",
            load_recent_history=lambda sid, agent=None: history or [],
            send_feishu_msg=lambda text, **kw: self.messages.append((text, kw)),
        )

    def test_enter_remote_without_history(self):
        ctx = self.make_ctx()
        enter_remote_mode("s", ["chat"], ctx)
        self.assertTrue(rm.remote_mode["s"])
        self.assertEqual(self.messages[0][0], "📱 已进入远程模式")

    def test_enter_remote_pushes_history(self):
        ctx = self.make_ctx([
            {"role": "user", "text": "hi"},
            {"role": "assistant", "text": "ok"},
        ])
        enter_remote_mode("s", ["chat"], ctx)
        texts = [m[0] for m in self.messages]
        self.assertTrue(any("最近对话" in t for t in texts))
        self.assertTrue(any("你：hi" in t for t in texts))
        self.assertTrue(any("Codex：ok" in t for t in texts))

    def test_ensure_and_exit_remote(self):
        ctx = self.make_ctx()
        ensure_remote_mode("s", ctx)
        exit_remote_mode("s", ["chat"], ctx, reason="本地输入")
        self.assertFalse(rm.remote_mode["s"])
        self.assertIn("本地输入", self.messages[-1][0])


if __name__ == "__main__":
    unittest.main()
