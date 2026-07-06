import io
import unittest
from contextlib import redirect_stdout

from opencode_session.event_watcher import BackgroundSessionEventWatcher
from opencode_session.validation_harness import DisposableValidationHarness, ValidationCheck


class FakeCleanupClient:
    def __init__(self):
        self.calls = []

    def delete_session_response(self, session_id):
        self.calls.append(("DELETE", session_id))
        return None

    def get_session(self, session_id):
        self.calls.append(("GET", session_id))
        return {"id": session_id}


class StoppableEventClient:
    def stream_events(self, _path, *, on_open=None, deadline=None, stop_event=None):
        if on_open is not None:
            on_open()
        while stop_event is not None and not stop_event.wait(0.01):
            pass
        if False:
            yield {}


class DisposableValidationHarnessTest(unittest.TestCase):
    def test_ordered_checks_record_returned_records(self):
        client = FakeCleanupClient()
        result = {"status": "active", "ok": False, "checks": {}, "cleanup": {"status": "queued"}}
        calls = []

        def first_check(_harness):
            calls.append("first")
            return {"status": "done", "value": 1}

        def second_check(_harness):
            calls.append("second")
            return {"status": "done", "value": 2}

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = DisposableValidationHarness(
                client,
                result,
                default_exit_code=69,
                cleanup_failure_message="cleanup failed",
            ).run(
                (ValidationCheck("first", first_check), ValidationCheck("second", second_check)),
                failure_types=(RuntimeError,),
                json_output=False,
                compact_formatter=lambda result: result["status"],
                failure_prefix="validation failed",
                print_error=self._unexpected_error,
                cleanup_summary_formatter=self._cleanup_summary,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(result["checks"]["first"], {"status": "done", "value": 1})
        self.assertEqual(result["checks"]["second"], {"status": "done", "value": 2})
        self.assertEqual(stdout.getvalue(), "done\n")

    def test_cleanup_verification_failure_fails_validation_and_suppresses_success_output(self):
        client = FakeCleanupClient()
        result = {"status": "active", "ok": False, "checks": {}, "cleanup": {"status": "queued"}}
        errors = []

        def validation_body(harness):
            harness.track_session("ses_leftover")
            result["checks"]["probe"] = {"status": "done"}

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = DisposableValidationHarness(
                client,
                result,
                default_exit_code=69,
                cleanup_failure_message="disposable validation session cleanup failed",
            ).run(
                validation_body,
                failure_types=(RuntimeError,),
                json_output=False,
                compact_formatter=self._unexpected_success_output,
                failure_prefix="validation failed",
                print_error=errors.append,
                cleanup_summary_formatter=self._cleanup_summary,
            )

        self.assertEqual(exit_code, 69)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(client.calls, [("DELETE", "ses_leftover"), ("GET", "ses_leftover")])
        self.assertEqual(
            errors,
            ["validation failed: disposable validation session cleanup failed; cleanup=failed deleted=0 verified=0"],
        )
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "disposable validation session cleanup failed")
        self.assertEqual(result["cleanup"]["status"], "failed")
        self.assertEqual(result["cleanup"]["deleted"], [])
        self.assertEqual(result["cleanup"]["verified"], [])
        self.assertEqual(result["cleanup"]["errors"][0]["session_id"], "ses_leftover")
        self.assertIn("delete verification failed", result["cleanup"]["errors"][0]["error"])
        self.assertIs(result["checks"]["cleanup"], result["cleanup"])

    def test_unexpected_exception_closes_event_watcher_and_deletes_sessions(self):
        client = FakeCleanupClient()
        result = {"status": "active", "ok": False, "checks": {}, "cleanup": {"status": "queued"}}
        watcher = None

        def validation_body(harness):
            nonlocal watcher
            harness.track_session("ses_leftover")
            watcher = harness.track_resource(
                BackgroundSessionEventWatcher(StoppableEventClient(), "/api/event", "ses_leftover")
            )
            watcher.start()
            watcher.wait_open(1.0)
            self.assertTrue(watcher.thread.is_alive())
            raise ValueError("boom")

        with self.assertRaisesRegex(ValueError, "boom"):
            DisposableValidationHarness(
                client,
                result,
                default_exit_code=69,
                cleanup_failure_message="cleanup failed",
            ).run(
                validation_body,
                failure_types=(RuntimeError,),
                json_output=False,
                compact_formatter=self._unexpected_success_output,
                failure_prefix="validation failed",
                print_error=self._unexpected_error,
                cleanup_summary_formatter=self._cleanup_summary,
            )

        self.assertIsNotNone(watcher)
        self.assertTrue(watcher.closed.is_set())
        self.assertFalse(watcher.thread.is_alive())
        self.assertEqual(client.calls, [("DELETE", "ses_leftover"), ("GET", "ses_leftover")])
        self.assertEqual(result["cleanup"]["status"], "failed")
        self.assertIs(result["checks"]["cleanup"], result["cleanup"])

    def _unexpected_success_output(self, result):
        raise AssertionError(f"unexpected success output for {result!r}")

    def _unexpected_error(self, message):
        raise AssertionError(f"unexpected error {message!r}")

    def _cleanup_summary(self, cleanup):
        return " ".join(
            [
                f"cleanup={cleanup.get('status')}",
                f"deleted={len(cleanup.get('deleted') or [])}",
                f"verified={len(cleanup.get('verified') or [])}",
            ]
        )


if __name__ == "__main__":
    unittest.main()
