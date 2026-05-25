import json
import os
import tempfile
import unittest

from state import BridgeState, load_state, save_state


class StateTests(unittest.TestCase):
    def test_load_missing_files(self):
        with tempfile.TemporaryDirectory() as d:
            state = load_state(
                os.path.join(d, "bindings.json"),
                os.path.join(d, "jsonl_ids.json"),
                os.path.join(d, "session_backends.json"),
            )
            self.assertEqual(state.chat_session_map, {})
            self.assertEqual(state.session_jsonl_id, {})
            self.assertEqual(state.session_backend, {})

    def test_save_and_load_state(self):
        with tempfile.TemporaryDirectory() as d:
            bind = os.path.join(d, "bindings.json")
            ids = os.path.join(d, "jsonl_ids.json")
            backend = os.path.join(d, "session_backends.json")
            original = BridgeState(
                chat_session_map={"chat": "session"},
                session_jsonl_id={"session": "sid"},
                session_backend={"session": "openai"},
            )
            save_state(original, bind, ids, backend)
            loaded = load_state(bind, ids, backend)
            self.assertEqual(loaded.chat_session_map, {"chat": "session"})
            self.assertEqual(loaded.session_jsonl_id, {"session": "sid"})
            self.assertEqual(loaded.session_backend, {"session": "codex"})

    def test_non_dict_json_loads_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            bind = os.path.join(d, "bindings.json")
            ids = os.path.join(d, "jsonl_ids.json")
            backend = os.path.join(d, "session_backends.json")
            for path in (bind, ids, backend):
                with open(path, "w") as f:
                    json.dump([], f)
            loaded = load_state(bind, ids, backend)
            self.assertEqual(loaded.chat_session_map, {})
            self.assertEqual(loaded.session_jsonl_id, {})
            self.assertEqual(loaded.session_backend, {})


if __name__ == "__main__":
    unittest.main()
