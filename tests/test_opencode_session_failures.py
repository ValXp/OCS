import os
import shlex
import socket
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "ocs"


class CliFailureContractTest(unittest.TestCase):
    def test_bad_server_url_exits_69_with_clear_stderr(self):
        result = run_ocs("capabilities", "--server", "not-a-url", "--json")

        context = format_completed_process(result)
        self.assertEqual(result.returncode, 69, context)
        self.assertEqual(result.stdout, "", context)
        self.assertIn("invalid OpenCode server URL", result.stderr, context)
        self.assertIn("not-a-url", result.stderr, context)
        self.assertNotIn("Traceback", result.stderr, context)

    def test_missing_server_url_exits_69_with_clear_stderr(self):
        result = run_ocs("capabilities", "--server", "", "--json", timeout_seconds=3)

        context = format_completed_process(result)
        self.assertEqual(result.returncode, 69, context)
        self.assertEqual(result.stdout, "", context)
        self.assertIn("invalid OpenCode server URL", result.stderr, context)
        self.assertNotIn("Traceback", result.stderr, context)

    def test_unreachable_local_server_exits_69_with_clear_stderr(self):
        server_url = unused_local_server_url()

        result = run_ocs("capabilities", "--server", server_url, "--json", timeout_seconds=3)

        context = format_completed_process(result)
        self.assertEqual(result.returncode, 69, context)
        self.assertEqual(result.stdout, "", context)
        self.assertIn("cannot reach OpenCode server", result.stderr, context)
        self.assertIn(server_url, result.stderr, context)
        self.assertNotIn("Traceback", result.stderr, context)

    def test_cleanup_unreachable_local_server_reports_failure_without_hanging(self):
        server_url = unused_local_server_url()

        result = run_ocs(
            "cleanup",
            "--directory",
            ".",
            "--prefix",
            "ocs-test-",
            "--server",
            server_url,
            "--json",
            timeout_seconds=3,
        )

        context = format_completed_process(result)
        self.assertEqual(result.returncode, 69, context)
        self.assertEqual(result.stdout, "", context)
        self.assertIn("cannot reach OpenCode server", result.stderr, context)
        self.assertIn(server_url, result.stderr, context)
        self.assertNotIn("Traceback", result.stderr, context)


def run_ocs(*args, timeout_seconds=None):
    command = [sys.executable, str(CLI), *args]
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout_seconds,
    )


def unused_local_server_url():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def format_completed_process(result):
    return "\n".join(
        [
            f"command: {shlex.join(str(part) for part in result.args)}",
            f"exit code: {result.returncode}",
            "stdout:",
            result.stdout or "(empty)",
            "stderr:",
            result.stderr or "(empty)",
        ]
    )


if __name__ == "__main__":
    unittest.main()
