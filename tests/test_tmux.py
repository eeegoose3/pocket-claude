import unittest
from unittest.mock import patch

import tmux


class TmuxTests(unittest.TestCase):
    @patch("tmux.subprocess.run")
    def test_tmux_run_success(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "out\n"
        self.assertEqual(tmux.tmux_run(["ls"]), (True, "out"))
        run.assert_called_once()

    @patch("tmux.tmux_run")
    def test_list_sessions(self, tmux_run):
        tmux_run.return_value = (True, "one\ntwo")
        self.assertEqual(tmux.list_sessions(), ["one", "two"])

    @patch("tmux.tmux_run")
    def test_send_keys_splits_text_and_enter(self, tmux_run):
        tmux.send_keys("s", "hello")
        tmux_run.assert_any_call(["send-keys", "-t", "s", "--", "hello"])
        tmux_run.assert_any_call(["send-keys", "-t", "s", "Enter"])

    @patch("tmux.tmux_run")
    def test_capture_pane(self, tmux_run):
        tmux_run.return_value = (True, "screen")
        self.assertEqual(tmux.capture_pane("s", lines=10), "screen")
        tmux_run.assert_called_once_with(["capture-pane", "-t", "s", "-p", "-S", "-10"])


if __name__ == "__main__":
    unittest.main()
