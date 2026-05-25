import unittest

import app


class AppRuntimeTests(unittest.TestCase):
    def test_runtime_initializes_state_and_contexts(self):
        rt = app.BridgeRuntime()
        self.assertEqual(rt.chat_session_map, {})
        self.assertEqual(rt.session_jsonl_id, {})
        self.assertEqual(rt.session_backend, {})
        self.assertIsNone(rt.lark_client)

        feishu_ctx = rt.build_feishu_context()
        self.assertIs(feishu_ctx.lark_client, None)
        self.assertIs(feishu_ctx.chat_session_map, rt.chat_session_map)

        command_ctx = rt.build_command_context()
        self.assertIs(command_ctx.chat_session_map, rt.chat_session_map)
        self.assertIs(command_ctx.session_backend, rt.session_backend)

        monitor_ctx = rt.build_monitor_context()
        self.assertIs(monitor_ctx.bridge_sent_time, rt.bridge_sent_time)

    def test_runtime_load_and_save_bindings_wrappers(self):
        rt = app.BridgeRuntime()
        loaded = app.BridgeState(
            chat_session_map={"chat": "session"},
            session_jsonl_id={"session": "sid"},
            session_backend={"session": "openai"},
        )
        saved = []
        old_load = app.load_state
        old_save = app.save_state
        try:
            app.load_state = lambda: loaded
            app.save_state = lambda state: saved.append(state)
            rt.load_bindings()
            self.assertEqual(rt.chat_session_map, {"chat": "session"})
            self.assertEqual(rt.session_jsonl_id, {"session": "sid"})
            self.assertEqual(rt.session_backend, {"session": "codex"})
            rt.save_bindings()
        finally:
            app.load_state = old_load
            app.save_state = old_save
        self.assertEqual(saved[0].chat_session_map, {"chat": "session"})


if __name__ == "__main__":
    unittest.main()
