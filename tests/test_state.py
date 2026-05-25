import json
import os
import sqlite3
import tempfile
import unittest

from state import BridgeState, load_state, save_state


class StateTests(unittest.TestCase):
    def test_load_missing_files(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "bridge_state.db")
            state = load_state(
                os.path.join(d, "bindings.json"),
                os.path.join(d, "jsonl_ids.json"),
                os.path.join(d, "session_backends.json"),
                db,
            )
            self.assertEqual(state.chat_session_map, {})
            self.assertEqual(state.session_jsonl_id, {})
            self.assertEqual(state.session_backend, {})
            self.assertTrue(os.path.exists(db))

    def test_save_and_load_state(self):
        with tempfile.TemporaryDirectory() as d:
            bind = os.path.join(d, "bindings.json")
            ids = os.path.join(d, "jsonl_ids.json")
            backend = os.path.join(d, "session_backends.json")
            db = os.path.join(d, "bridge_state.db")
            original = BridgeState(
                chat_session_map={"chat": "session"},
                session_jsonl_id={"session": "sid"},
                session_backend={"session": "openai"},
                remote_mode={"session": True},
                session_runtime={"session": {"jsonl_path": "/tmp/a.jsonl", "jsonl_offset": 123, "last_message_id": "m1"}},
            )
            save_state(original, bind, ids, backend, db)
            loaded = load_state(bind, ids, backend, db)
            self.assertEqual(loaded.chat_session_map, {"chat": "session"})
            self.assertEqual(loaded.session_jsonl_id, {"session": "sid"})
            self.assertEqual(loaded.session_backend, {"session": "codex"})
            self.assertEqual(loaded.remote_mode, {"session": True})
            self.assertEqual(loaded.session_runtime["session"]["jsonl_path"], "/tmp/a.jsonl")
            self.assertEqual(loaded.session_runtime["session"]["jsonl_offset"], 123)
            self.assertEqual(loaded.session_runtime["session"]["last_message_id"], "m1")

    def test_non_dict_json_loads_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            bind = os.path.join(d, "bindings.json")
            ids = os.path.join(d, "jsonl_ids.json")
            backend = os.path.join(d, "session_backends.json")
            db = os.path.join(d, "bridge_state.db")
            for path in (bind, ids, backend):
                with open(path, "w") as f:
                    json.dump([], f)
            loaded = load_state(bind, ids, backend, db)
            self.assertEqual(loaded.chat_session_map, {})
            self.assertEqual(loaded.session_jsonl_id, {})
            self.assertEqual(loaded.session_backend, {})

    def test_migrates_legacy_json_to_sqlite(self):
        with tempfile.TemporaryDirectory() as d:
            bind = os.path.join(d, "bindings.json")
            ids = os.path.join(d, "jsonl_ids.json")
            backend = os.path.join(d, "session_backends.json")
            db = os.path.join(d, "bridge_state.db")
            with open(bind, "w") as f:
                json.dump({"chat": "session"}, f)
            with open(ids, "w") as f:
                json.dump({"session": "sid"}, f)
            with open(backend, "w") as f:
                json.dump({"session": "openai"}, f)

            loaded = load_state(bind, ids, backend, db)
            self.assertEqual(loaded.chat_session_map, {"chat": "session"})
            self.assertEqual(loaded.session_jsonl_id, {"session": "sid"})
            self.assertEqual(loaded.session_backend, {"session": "codex"})

            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM chat_bindings").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT backend FROM session_backends").fetchone()[0], "codex")
            finally:
                conn.close()

    def test_sqlite_wins_over_stale_legacy_json_after_migration(self):
        with tempfile.TemporaryDirectory() as d:
            bind = os.path.join(d, "bindings.json")
            ids = os.path.join(d, "jsonl_ids.json")
            backend = os.path.join(d, "session_backends.json")
            db = os.path.join(d, "bridge_state.db")
            save_state(BridgeState(chat_session_map={"chat": "fresh"}), bind, ids, backend, db)
            with open(bind, "w") as f:
                json.dump({"chat": "stale"}, f)

            loaded = load_state(bind, ids, backend, db)
            self.assertEqual(loaded.chat_session_map, {"chat": "fresh"})


if __name__ == "__main__":
    unittest.main()
