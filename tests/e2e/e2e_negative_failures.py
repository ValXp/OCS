import unittest

from harness import format_completed_process, run_ocs, unused_local_server_url


class NegativeFailureE2ETest(unittest.TestCase):
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
            "ocs-e2e-",
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


if __name__ == "__main__":
    unittest.main()
