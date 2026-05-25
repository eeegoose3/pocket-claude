import json
import tempfile
import unittest

import history


class HistoryTests(unittest.TestCase):
    def test_load_recent_history_pairs_last_rounds(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f:
            rows = [
                {"type": "event_msg", "payload": {"type": "user_message", "message": "u1"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "a1"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "u2"}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "a2"}},
            ]
            for row in rows:
                f.write(json.dumps(row) + "\n")
            f.flush()
            old = history.find_log_by_session_id
            try:
                history.find_log_by_session_id = lambda sid, agent=None: f.name
                self.assertEqual(
                    history.load_recent_history("sid", rounds=1, agent="codex"),
                    [
                        {"role": "user", "text": "u2"},
                        {"role": "assistant", "text": "a2"},
                    ],
                )
            finally:
                history.find_log_by_session_id = old

    def test_load_recent_history_missing_log(self):
        old = history.find_log_by_session_id
        try:
            history.find_log_by_session_id = lambda sid, agent=None: None
            self.assertEqual(history.load_recent_history("missing"), [])
        finally:
            history.find_log_by_session_id = old


if __name__ == "__main__":
    unittest.main()
