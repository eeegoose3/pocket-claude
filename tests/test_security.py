import os
import tempfile
import unittest

import security


class SecurityTests(unittest.TestCase):
    def test_session_name_validation(self):
        self.assertIsNone(security.validate_session_name("good_name-1.2"))
        self.assertIsNotNone(security.validate_session_name("bad;name"))
        self.assertIsNotNone(security.validate_session_name(""))

    def test_approval_token(self):
        self.assertTrue(security.approval_token_ok(["/y"], token=""))
        self.assertTrue(security.approval_token_ok(["/y", "abc"], token="abc"))
        self.assertFalse(security.approval_token_ok(["/y", "wrong"], token="abc"))

    def test_file_allowlist(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ok.txt")
            with open(path, "w") as f:
                f.write("ok")
            allowed, resolved = security.is_user_file_allowed(path, allow_dirs=[d])
            self.assertTrue(allowed)
            self.assertEqual(resolved, os.path.realpath(path))
            denied, _ = security.is_user_file_allowed("/etc/passwd", allow_dirs=[d])
            self.assertFalse(denied)

    def test_whitelist(self):
        self.assertTrue(security.whitelist_allows_sender("u1", "u1", allow_all=False))
        self.assertFalse(security.whitelist_allows_sender("u1", "u2", allow_all=False))
        self.assertFalse(security.whitelist_allows_sender(None, "u2", allow_all=False))
        self.assertTrue(security.whitelist_allows_sender(None, "u2", allow_all=True))

    def test_doctor_reports_parser_compatibility(self):
        report = security.doctor_report("app", "secret", "user", "/missing/claude", "/missing/codex")
        self.assertIn("State store", report)
        self.assertIn("SQLite", report)
        self.assertIn("Parser compatibility", report)
        self.assertIn("claude-code-jsonl-v1", report)
        self.assertIn("codex-rollout-jsonl-v1", report)


if __name__ == "__main__":
    unittest.main()
