import unittest

from opencode_session.cli_policy import (
    DEFAULT_SERVER_URL,
    EX_ABORTED,
    EX_BLOCKED,
    EX_PARTIAL,
    EX_TIMEOUT,
    EX_UNAVAILABLE,
    EX_UNSUPPORTED,
    WORKER_EXIT_CODE_BY_STATUS,
    exit_code_for_run,
    exit_code_for_status,
    server_default,
)
from opencode_session.worker_state import refresh_run_summary


class CliPolicyTest(unittest.TestCase):
    def test_server_default_prefers_new_env_name_then_legacy_then_constant(self):
        self.assertEqual(server_default({}), DEFAULT_SERVER_URL)
        self.assertEqual(server_default({"OPENCODE_SERVER": "http://legacy.example"}), "http://legacy.example")
        self.assertEqual(
            server_default(
                {
                    "OPENCODE_SERVER_URL": "http://current.example",
                    "OPENCODE_SERVER": "http://legacy.example",
                }
            ),
            "http://current.example",
        )

    def test_worker_status_exit_code_mapping_preserves_existing_process_policy(self):
        self.assertEqual(EX_UNAVAILABLE, 69)
        self.assertEqual(EX_UNSUPPORTED, 70)
        self.assertEqual(EX_TIMEOUT, 124)
        self.assertEqual(EX_PARTIAL, 1)
        self.assertEqual(EX_BLOCKED, 75)
        self.assertEqual(EX_ABORTED, 130)
        self.assertEqual(
            WORKER_EXIT_CODE_BY_STATUS,
            {
                "blocked": EX_BLOCKED,
                "done": 0,
                "failed": EX_UNAVAILABLE,
                "timeout": EX_TIMEOUT,
                "aborted": EX_ABORTED,
            },
        )

    def test_exit_code_for_status_maps_domain_status_to_cli_policy(self):
        self.assertEqual(exit_code_for_status("done"), 0)
        self.assertEqual(exit_code_for_status("blocked"), EX_BLOCKED)
        self.assertEqual(exit_code_for_status("failed"), EX_UNAVAILABLE)
        self.assertEqual(exit_code_for_status("failed", partial_success=True), EX_PARTIAL)
        self.assertEqual(exit_code_for_status("timeout"), EX_TIMEOUT)
        self.assertEqual(exit_code_for_status("aborted"), EX_ABORTED)
        self.assertEqual(exit_code_for_status("queued"), EX_UNAVAILABLE)

    def test_exit_code_for_run_detects_partial_worker_success(self):
        run = {
            "status": "failed",
            "workers": {
                "build": {"id": "build", "prompt": "Build", "lifecycle_state": "done_collect"},
                "review": {"id": "review", "prompt": "Review", "lifecycle_state": "failed_terminal"},
            },
        }

        self.assertEqual(exit_code_for_run(run), EX_PARTIAL)

    def test_exit_code_for_run_uses_refreshed_run_status_precedence(self):
        run = {
            "workers": {
                "build": {"id": "build", "prompt": "Build", "lifecycle_state": "timeout_terminal"},
                "review": {"id": "review", "prompt": "Review", "lifecycle_state": "aborted"},
                "test": {"id": "test", "prompt": "Test", "lifecycle_state": "failed_terminal"},
            }
        }

        refresh_run_summary(run)

        self.assertEqual(run["status"], "failed")
        self.assertEqual(exit_code_for_run(run), EX_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
