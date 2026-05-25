import contextlib
import io
import os
import tempfile
import unittest

import cli


class CliTests(unittest.TestCase):
    def test_version_command(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = cli.main(["version"])
        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue().strip(), cli.VERSION)

    def test_no_command_prints_help(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = cli.main([])
        self.assertEqual(rc, 0)
        self.assertIn("usage: pocket-claude", stdout.getvalue())

    def test_init_creates_env_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            env_path = os.path.join(d, ".env")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = cli.main(["init", "--env", env_path])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(env_path))
            with open(env_path) as f:
                self.assertIn("APP_ID=", f.read())

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = cli.main(["init", "--env", env_path])
            self.assertEqual(rc, 1)
            self.assertIn("already exists", stderr.getvalue())

    def test_doctor_loads_env_file_without_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            env_path = os.path.join(d, ".env")
            secret = "super-secret-value"
            with open(env_path, "w") as f:
                f.write(
                    "APP_ID=cli_test\n"
                    f"APP_SECRET={secret}\n"
                    "ALLOWED_USER_ID=ou_test\n"
                    "FILE_ALLOW_DIRS=/tmp\n"
                    "APPROVAL_TOKEN=token123\n"
                )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = cli.main(["doctor", "--env", env_path])

            report = stdout.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("Bridge Doctor", report)
            self.assertIn("Feishu credentials", report)
            self.assertIn("APP_ID/APP_SECRET 已配置", report)
            self.assertIn("/file allowlist", report)
            self.assertIn("Approval token", report)
            self.assertIn("Parser compatibility", report)
            self.assertNotIn(secret, report)


if __name__ == "__main__":
    unittest.main()
