import unittest

import app


class FakeAdapter:
    name = "fake"

    def __init__(self):
        self.messages = []
        self.files = []
        self.chats = []
        self.events = []
        self.catchups = 0

    def send_message(self, text, ctx, target_chat_id=None, use_card=None):
        self.messages.append((text, ctx.default_chat_id, target_chat_id, use_card))

    def send_file(self, file_path, ctx, target_chat_id=None):
        self.files.append((file_path, target_chat_id))

    def create_chat(self, name, ctx):
        self.chats.append(name)
        return f"chat_{name}"

    def on_message(self, data, ctx):
        self.events.append((data, ctx.default_chat_id))

    def catchup_missed_messages(self, ctx):
        self.catchups += 1


class AppRuntimeTests(unittest.TestCase):
    def test_runtime_initializes_state_and_contexts(self):
        rt = app.BridgeRuntime()
        self.assertEqual(rt.chat_session_map, {})
        self.assertEqual(rt.session_jsonl_id, {})
        self.assertEqual(rt.session_backend, {})
        self.assertIsNone(rt.im_client)

        feishu_ctx = rt.build_im_context()
        self.assertIs(feishu_ctx.client, None)
        self.assertIs(feishu_ctx.chat_session_map, rt.chat_session_map)

        command_ctx = rt.build_command_context()
        self.assertIs(command_ctx.chat_session_map, rt.chat_session_map)
        self.assertIs(command_ctx.session_backend, rt.session_backend)

        monitor_ctx = rt.build_monitor_context()
        self.assertIs(monitor_ctx.bridge_sent_time, rt.bridge_sent_time)
        self.assertIs(monitor_ctx.session_runtime, rt.session_runtime)

    def test_runtime_load_and_save_bindings_wrappers(self):
        rt = app.BridgeRuntime()
        loaded = app.BridgeState(
            chat_session_map={"chat": "session"},
            session_jsonl_id={"session": "sid"},
            session_backend={"session": "openai"},
            remote_mode={"session": True},
            session_runtime={"session": {"jsonl_offset": 10}},
        )
        saved = []
        old_load = app.load_state
        old_save = app.save_state
        app.remote_mode.clear()
        try:
            app.load_state = lambda: loaded
            app.save_state = lambda state: saved.append(state)
            rt.load_bindings()
            self.assertEqual(rt.chat_session_map, {"chat": "session"})
            self.assertEqual(rt.session_jsonl_id, {"session": "sid"})
            self.assertEqual(rt.session_backend, {"session": "codex"})
            self.assertEqual(rt.session_runtime, {"session": {"jsonl_offset": 10}})
            self.assertEqual(app.remote_mode, {"session": True})
            rt.save_bindings()
        finally:
            app.load_state = old_load
            app.save_state = old_save
            app.remote_mode.clear()
        self.assertEqual(saved[0].chat_session_map, {"chat": "session"})
        self.assertEqual(saved[0].remote_mode, {"session": True})
        self.assertEqual(saved[0].session_runtime, {"session": {"jsonl_offset": 10}})

    def test_runtime_uses_injected_im_adapter(self):
        adapter = FakeAdapter()
        rt = app.BridgeRuntime(im_adapter=adapter)
        rt.reply_chat_id = "default"

        rt.send_im_msg("hello", target_chat_id="chat_1", use_card=False)
        rt.send_im_file("/tmp/a.txt", target_chat_id="chat_1")
        chat_id = rt.create_im_chat("work")
        rt.on_message("event")
        rt.catchup_missed_messages()

        self.assertEqual(adapter.messages, [("hello", "default", "chat_1", False)])
        self.assertEqual(adapter.files, [("/tmp/a.txt", "chat_1")])
        self.assertEqual(adapter.chats, ["work"])
        self.assertEqual(chat_id, "chat_work")
        self.assertEqual(adapter.events, [("event", "default")])
        self.assertEqual(adapter.catchups, 1)


if __name__ == "__main__":
    unittest.main()
